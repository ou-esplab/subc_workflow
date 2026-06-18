#!/usr/bin/env python3
"""
Compute per-MMDD percentile threshold files from SubX hindcast data.

Reads hindcast files from:
  {rt_root}/{group}-{model}/hindcast/{var}/{var}_{group}-{model}_{YYYYMMDD}.daily.nc

Writes per-MMDD threshold files to:
  {hc_root}/{var}{lev}/daily/percentiles/{model}-{group}/{var}_{model}-{group}/
    {var}_{group}-{model}_{MMDD}.{pct}p.nc

Each output file has dimensions (month_day: 1, L, Y, X) matching the format
expected by compute_exceedance in utils/exceedance_utils.py.

Usage
-----
  # Dry run to preview what would be written
  python static/make_subx_percentiles.py \\
      --config config.yaml --model ECCC-GEPS8 --var pr --lev sfc --dry-run

  # Compute and write 95th percentile files
  python static/make_subx_percentiles.py \\
      --config config.yaml --model ECCC-GEPS8 --var pr --lev sfc

  # Different percentile or year range
  python static/make_subx_percentiles.py \\
      --config config.yaml --model RSMAS-CCSM4 --var pr --lev sfc \\
      --percentile 90 --years 1999 2000 2001 2002 2003 2004 2005

  # Overwrite existing files
  python static/make_subx_percentiles.py \\
      --config config.yaml --model NCEP-CFSv2 --var pr --lev sfc --overwrite
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr
import yaml
from tqdm import tqdm


DEFAULT_YEARS = list(range(1999, 2021))


def _load_cfg(config_path: str) -> dict:
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _parse_model(model_id: str) -> tuple[str, str]:
    if "-" not in model_id:
        raise ValueError(f"--model must be GROUP-MODEL format (e.g. ECCC-GEPS8), got: {model_id!r}")
    group, model = model_id.split("-", 1)
    return group, model


def _to_rate(ds: xr.Dataset, var: str) -> xr.Dataset:
    """Convert accumulated precipitation to kg m-2 s-1 if needed (e.g. EMC stores kg m-2)."""
    if var != "pr":
        return ds
    units = ds[var].attrs.get("units", "").lower().replace(" ", "")
    is_rate = any(t in units for t in ["s-1", "/s", "s^-1"])
    if not is_rate and units:
        ds[var] = ds[var] / 86400.0
        ds[var].attrs["units"] = "kg m-2 s-1"
    return ds


def _fold_s_into_m(ds: xr.Dataset) -> xr.Dataset:
    """Treat each S sub-daily init as an additional ensemble member.

    S=1: drop S (existing behaviour).
    S>1 (e.g. CFSv2 00z/06z/12z/18z): fold into M so all S slots contribute
    to the percentile sample pool as pseudo-members.
    """
    if "S" not in ds.dims:
        return ds
    if ds.sizes["S"] == 1:
        return ds.isel(S=0, drop=True)
    # Assign integer coords so concat across years won't try to align datetimes
    ds = ds.assign_coords(S=np.arange(ds.sizes["S"]))
    if "M" in ds.dims:
        ds = ds.assign_coords(M=np.arange(ds.sizes["M"]))
        ds = ds.stack(_sm=("S", "M")).reset_index("_sm").rename({"_sm": "M"})
    else:
        ds = ds.rename({"S": "M"}).assign_coords(M=np.arange(ds.sizes["S"]))
    return ds


def _is_pressure_level(lev: str) -> bool:
    try:
        int(lev)
        return True
    except ValueError:
        return False


def _doy_from_mmdd(mmdd: str) -> int:
    """Day-of-year for MM-DD using a fixed leap year so Feb 29 is valid."""
    return pd.Timestamp(f"2000-{mmdd}").day_of_year


def _circular_day_distance(d1: int, d2: int) -> int:
    diff = abs(d1 - d2)
    return min(diff, 366 - diff)


def compute_percentiles(
    rt_root: str,
    hc_root: str,
    group: str,
    model: str,
    var: str,
    lev: str,
    percentile: int,
    years: list[int],
    window: int,
    overwrite: bool,
    dry_run: bool,
) -> int:
    model_id = f"{group}-{model}"
    quantile = percentile / 100.0

    out_dir = (
        Path(hc_root)
        / f"{var}{lev}"
        / "daily"
        / "percentiles"
        / f"{model}-{group}"
        / f"{var}_{model}-{group}"
    )

    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. Load hindcast files and group by calendar MM-DD ────────────────────
    # Each entry: exact MMDD of the hindcast init date.
    # Phase 2 then pools samples from ±window days around each target MMDD,
    # matching the effective ±15-day window used by the climatology smoothing.
    # Store numpy arrays (not lazy dask) to avoid expensive concat+rechunk
    # overhead in the per-MMDD quantile loop.
    calendardates: dict[str, list[np.ndarray]] = {}
    lead_coords: list | None = None  # L coordinate values from first loaded file
    n_loaded = 0
    n_missing = 0

    print(f"[{model_id}] Loading hindcast {var} for {len(years)} years...")
    for year in tqdm(years, desc="Years"):
        for month in range(1, 13):
            for day in range(1, 32):
                try:
                    ts = pd.Timestamp(f"{year}-{month:02d}-{day:02d}")
                except ValueError:
                    continue

                path = (
                    f"{rt_root}/{model_id}/hindcast/"
                    f"{var}/{var}_{model_id}_{ts.strftime('%Y%m%d')}.daily.nc"
                )
                if not os.path.exists(path):
                    n_missing += 1
                    continue

                ds = xr.open_dataset(path)

                # Select pressure level when var has a P dimension (e.g. zg)
                if _is_pressure_level(lev) and "P" in ds.dims:
                    ds = ds.sel(P=int(lev))

                ds = _to_rate(ds, var)    # convert accumulated pr to rate (e.g. EMC kg m-2 → kg m-2 s-1)
                ds = _fold_s_into_m(ds)  # fold S>1 sub-daily inits into M (e.g. CFSv2 S=4)

                mmdd = ts.strftime("%m-%d")
                # Expand into individual members and eagerly materialize to numpy.
                # Avoids building a massive dask graph across thousands of lazy arrays.
                n_members = ds.sizes.get("M", 1)
                for i in range(n_members):
                    member_da = ds[var].isel(M=i, drop=True) if "M" in ds.dims else ds[var]
                    arr = member_da.values.astype(np.float32)
                    if lead_coords is None and "L" in member_da.coords:
                        lead_coords = list(member_da.coords["L"].values)
                    calendardates.setdefault(mmdd, []).append(arr)
                n_loaded += 1

    print(f"  loaded={n_loaded}  missing={n_missing}")
    print(f"  grouped into {len(calendardates)} calendar days")

    if not calendardates:
        print("[ERROR] No data loaded — aborting.")
        return 1

    # Precompute DOY for each loaded MMDD to speed up window lookups.
    loaded_doy: dict[str, int] = {mmdd: _doy_from_mmdd(mmdd) for mmdd in calendardates}

    # ── 2. Compute percentile per MM-DD and write output ──────────────────────
    # Iterate over all 366 calendar days. For each, pool samples from all
    # loaded MMDDs within ±window days (circular calendar distance).
    all_target_mmdd: list[str] = []
    for m in range(1, 13):
        for d in range(1, 32):
            try:
                all_target_mmdd.append(pd.Timestamp(f"2000-{m:02d}-{d:02d}").strftime("%m-%d"))
            except ValueError:
                continue

    n_wrote = 0
    n_existed = 0

    print(f"\n[{model_id}] Computing {percentile}th percentile (window=±{window}d) and writing output...")
    for target_mmdd in tqdm(all_target_mmdd, desc="MM-DD"):
        mmdd_str = target_mmdd.replace("-", "")
        out_file = out_dir / f"{var}_{group}-{model}_{mmdd_str}.{percentile}p.nc"

        if out_file.exists() and not overwrite:
            n_existed += 1
            continue

        # Pool samples from all loaded MMDDs within ±window days.
        target_doy = _doy_from_mmdd(target_mmdd)
        dslist = []
        for src_mmdd, members in calendardates.items():
            if _circular_day_distance(loaded_doy[src_mmdd], target_doy) <= window:
                dslist.extend(members)

        if not dslist:
            continue  # no data within window for this MMDD

        if dry_run:
            print(f"  [DRY-RUN] {out_file}  (n_samples={len(dslist)})")
            n_wrote += 1
            continue

        # Stack numpy arrays and compute percentile directly — avoids the
        # expensive dask concat+rechunk path. Transpose to (L, Y, X, n_samples)
        # so the sample axis is contiguous in memory, which is much faster for
        # nanpercentile than computing along axis=0 of (n_samples, L, Y, X).
        arr = np.stack(dslist, axis=0)  # (n_samples, L, Y, X)
        n_leads = arr.shape[1]
        arr_t = np.ascontiguousarray(arr.transpose(1, 2, 3, 0))  # (L, Y, X, n_samples)
        n = arr_t.shape[-1]
        k = int(np.round(quantile * (n - 1)))
        # Replace NaN with inf so they sort to the end, enabling O(n) partial sort.
        # Grid points where the k-th partition value is inf were all-NaN → set NaN.
        arr_filled = arr_t.copy()
        arr_filled[np.isnan(arr_filled)] = np.inf
        pct_arr = np.partition(arr_filled, k, axis=-1)[..., k]
        pct_arr[~np.isfinite(pct_arr)] = np.nan
        pct_da = xr.DataArray(pct_arr, dims=("L", "Y", "X"))

        # Assemble output: dims (month_day: 1, L, Y, X)
        l_vals = lead_coords[:n_leads] if lead_coords is not None else list(range(n_leads))
        da = pct_da.assign_coords(L=l_vals)
        da = da.expand_dims(month_day=[target_mmdd])
        ds_out = da.to_dataset(name=var)

        # Atomic write via temp file
        tmp = out_file.with_suffix(".tmp.nc")
        ds_out.to_netcdf(tmp)
        tmp.rename(out_file)
        n_wrote += 1

    verb = "would write" if dry_run else "wrote"
    print(f"\n  {verb} {n_wrote} files, {n_existed} already existed (skipped)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Compute per-MMDD SubX percentile threshold files from hindcast data"
    )
    ap.add_argument("--config",     required=True,
                    help="Path to config.yaml")
    ap.add_argument("--model",      required=True,
                    help="Model ID as GROUP-MODEL (e.g. ECCC-GEPS8, RSMAS-CCSM4)")
    ap.add_argument("--var",        required=True,
                    help="Variable name (e.g. pr, tas, zg)")
    ap.add_argument("--lev",        required=True,
                    help="Level suffix used in output directory (e.g. sfc, 2m, 500)")
    ap.add_argument("--percentile", type=int, default=95,
                    help="Percentile value 0-100 (default: 95)")
    ap.add_argument("--window",     type=int, default=15,
                    help="Half-width in days for pooling nearby hindcast dates (default: 15, "
                         "matching the ±15-day effective window used by the climatology)")
    ap.add_argument("--years",      type=int, nargs="+", default=None,
                    help=f"Hindcast years to use (default: {DEFAULT_YEARS[0]}-{DEFAULT_YEARS[-1]})")
    ap.add_argument("--overwrite",  action="store_true",
                    help="Overwrite existing output files")
    ap.add_argument("--dry-run",    action="store_true",
                    help="Preview output paths without writing any files")
    args = ap.parse_args()

    cfg = _load_cfg(args.config)
    paths = cfg.get("paths") or {}
    rt_root = paths.get("rt_root", "")
    hc_root = paths.get("hc_root", "")

    if not rt_root:
        print("[ERROR] paths.rt_root not set in config.yaml")
        return 1
    if not hc_root:
        print("[ERROR] paths.hc_root not set in config.yaml")
        return 1
    if not 0 < args.percentile < 100:
        print(f"[ERROR] --percentile must be between 1 and 99, got {args.percentile}")
        return 1

    group, model = _parse_model(args.model)
    years = args.years or DEFAULT_YEARS

    return compute_percentiles(
        rt_root=rt_root,
        hc_root=hc_root,
        group=group,
        model=model,
        var=args.var,
        lev=args.lev,
        percentile=args.percentile,
        window=args.window,
        years=years,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    raise SystemExit(main())
