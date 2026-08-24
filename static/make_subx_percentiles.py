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
import json
import multiprocessing as mp
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr
import yaml
from tqdm import tqdm


DEFAULT_YEARS = list(range(1999, 2021))
DEFAULT_WORKERS = 8

# Populated by compute_percentiles() before the process pool is forked, so
# worker processes inherit it via copy-on-write instead of it being pickled
# and sent through IPC (calendardates holds many GB of numpy arrays for
# models like CFSv2 -- pickling that per-task would be far slower than the
# percentile computation itself). Relies on the "fork" start method, which
# is why the pool below is created with mp.get_context("fork") explicitly
# rather than trusting the platform default (spawn on some platforms would
# silently make each worker reload this module with an empty dict).
_WORKER_STATE: dict = {}


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


def _compute_and_write_one_mmdd(target_mmdd: str) -> str:
    """Worker: pool samples for one target MM-DD, compute the percentile, write it.

    Reads shared inputs from _WORKER_STATE (inherited via fork, not pickled).
    Runs in a separate process so the 366 independent MM-DD outputs -- each
    reading the same in-memory hindcast pool but writing its own file -- can
    be computed in parallel instead of serially in one process.
    """
    calendardates = _WORKER_STATE["calendardates"]
    loaded_doy = _WORKER_STATE["loaded_doy"]
    lead_coords = _WORKER_STATE["lead_coords"]
    window = _WORKER_STATE["window"]
    quantile = _WORKER_STATE["quantile"]
    out_dir = _WORKER_STATE["out_dir"]
    var = _WORKER_STATE["var"]
    group = _WORKER_STATE["group"]
    model = _WORKER_STATE["model"]
    percentile = _WORKER_STATE["percentile"]
    overwrite = _WORKER_STATE["overwrite"]
    dry_run = _WORKER_STATE["dry_run"]

    mmdd_str = target_mmdd.replace("-", "")
    out_file = out_dir / f"{var}_{group}-{model}_{mmdd_str}.{percentile}p.nc"

    if out_file.exists() and not overwrite:
        return "existed"

    # Pool samples from all loaded MMDDs within ±window days.
    target_doy = _doy_from_mmdd(target_mmdd)
    dslist = []
    for src_mmdd, members in calendardates.items():
        if _circular_day_distance(loaded_doy[src_mmdd], target_doy) <= window:
            dslist.extend(members)

    if not dslist:
        return "no_data"

    if dry_run:
        return "dry_run"

    # Truncate every pooled sample to the shortest lead length in the pool
    # before stacking. Samples come from different hindcast files (different
    # years, different MM-DDs within the window), and while most models'
    # files share one native lead length, some (e.g. GMAO-GEOS_V3) vary by a
    # day or two file-to-file -- np.stack requires identical shapes, so an
    # unguarded stack crashes on the first mismatched pool (confirmed: 2026-
    # 08-19 Phase 2 run, "ValueError: all input arrays must have the same
    # shape"). Truncating rather than padding avoids fabricating data; losing
    # a day or two of lead length off samples that are already being pooled
    # across a +/-15-day window is immaterial to the climatological result.
    min_leads = min(a.shape[0] for a in dslist)
    dslist = [a[:min_leads] if a.shape[0] > min_leads else a for a in dslist]

    # Stack numpy arrays and compute percentile directly — avoids the
    # expensive dask concat+rechunk path. Transpose to (L, Y, X, n_samples)
    # so the sample axis is contiguous in memory, which is much faster for
    # nanpercentile than computing along axis=0 of (n_samples, L, Y, X).
    arr = np.stack(dslist, axis=0)  # (n_samples, L, Y, X)
    n_leads = arr.shape[1]
    arr_t = np.ascontiguousarray(arr.transpose(1, 2, 3, 0))  # (L, Y, X, n_samples)
    del arr
    n = arr_t.shape[-1]
    k = int(np.round(quantile * (n - 1)))
    # Replace NaN with inf so they sort to the end, enabling O(n) partial sort.
    # Grid points where the k-th partition value is inf were all-NaN → set NaN.
    # Done in-place on arr_t (already a fresh copy from the transpose above)
    # rather than a further .copy(), to avoid a second multi-GB allocation
    # per MM-DD -- this matters more now that several MM-DDs run at once.
    arr_t[np.isnan(arr_t)] = np.inf
    pct_arr = np.partition(arr_t, k, axis=-1)[..., k]
    pct_arr[~np.isfinite(pct_arr)] = np.nan
    pct_da = xr.DataArray(pct_arr, dims=("L", "Y", "X"))

    # Assemble output: dims (month_day: 1, L, Y, X)
    # Write explicit ascending Y/X coordinates (matching the sortby("Y")
    # normalization in the loader) instead of leaving them as bare, unlabeled
    # dimensions -- compute_exceedance previously had to infer lat labels
    # from the forecast field's orientation, which silently mirrored
    # north/south for any source model with a different native order.
    n_lat, n_lon = pct_arr.shape[1], pct_arr.shape[2]
    y_vals = np.linspace(-90.0, 90.0, n_lat)
    x_vals = np.linspace(0.0, 360.0 - 360.0 / n_lon, n_lon)
    l_vals = lead_coords[:n_leads] if lead_coords is not None else list(range(n_leads))
    da = pct_da.assign_coords(L=l_vals, Y=y_vals, X=x_vals)
    da = da.expand_dims(month_day=[target_mmdd])
    ds_out = da.to_dataset(name=var)

    # Atomic write via temp file (unique per-process suffix so concurrent
    # workers never collide on the same temp path).
    tmp = out_file.with_suffix(f".tmp{os.getpid()}.nc")
    ds_out.to_netcdf(tmp)
    tmp.rename(out_file)
    return "wrote"


def _period_confirmed_in_manifest(
    hc_root: str, group: str, model: str, var: str, lev: str, percentile: int,
    start_year: int, end_year: int,
) -> bool:
    """Check CLIMATOLOGY_MANIFEST.jsonl for a completed percentile record
    covering this exact (group-model, var, lev, percentile) at this exact
    start/end year -- see the identically-named helper in make_subx_climo.py
    for why file existence alone is not sufficient (output filenames carry
    no year-range info, so existing files could be stale leftovers from a
    different period)."""
    manifest_path = Path(hc_root) / "CLIMATOLOGY_MANIFEST.jsonl"
    if not manifest_path.exists():
        return False
    entry_label = f"{group}-{model} {var} {lev}.{percentile}"
    try:
        with open(manifest_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    rec.get("stage") == "percentile"
                    and rec.get("start_year") == start_year
                    and rec.get("end_year") == end_year
                    and entry_label in (rec.get("entries") or [])
                ):
                    return True
    except OSError:
        return False
    return False


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
    n_workers: int = DEFAULT_WORKERS,
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

    # Early-exit if every output file already exists AND the manifest
    # confirms they were built for this exact year set: the per-MMDD skip
    # check in _compute_and_write_one_mmdd only ever skipped the cheap write
    # step -- the expensive "Loading hindcast files" pass below it (often
    # the dominant cost, e.g. hours for CFSv2) still ran in full even when
    # every target file was going to be skipped anyway. The 366 target
    # MM-DDs are always the same fixed calendar days regardless of what's
    # actually loaded, so existence alone can't distinguish "already done for
    # this year set" from "stale leftovers from a different one" -- the
    # manifest check is required for correctness, not just an optimization
    # (see _period_confirmed_in_manifest's docstring). Assumes `years` is a
    # contiguous range (min/max), matching how run_all_static.sh always
    # builds it and how the manifest itself only records start/end year.
    if not overwrite and not dry_run and years:
        all_target_mmdd_early: list[str] = []
        for m in range(1, 13):
            for d in range(1, 32):
                try:
                    all_target_mmdd_early.append(pd.Timestamp(f"2000-{m:02d}-{d:02d}").strftime("%m-%d"))
                except ValueError:
                    continue
        files_exist = all(
            (out_dir / f"{var}_{group}-{model}_{mmdd.replace('-', '')}.{percentile}p.nc").exists()
            for mmdd in all_target_mmdd_early
        )
        if files_exist and _period_confirmed_in_manifest(
            hc_root, group, model, var, lev, percentile, min(years), max(years)
        ):
            print(f"[{model_id}] All {len(all_target_mmdd_early)} output files already exist and "
                  f"are confirmed (manifest) for {min(years)}-{max(years)} p{percentile} -- "
                  f"skipping (use --overwrite to force recomputation).")
            return 0

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

                # Cap lead time at 45 days from init, matching the SubX
                # weekly-product convention every other model in this archive
                # already falls within natively (~39-44 days). GEOS_V3's raw
                # hindcast runs much longer (60-92 days normally, and a real
                # but unwanted anomaly on a few 2003-02 dates extends 300+
                # days past init) -- applied here rather than at download time
                # so the raw archive stays at full fidelity and this cap
                # stays a one-line, easily revisited choice. Incidentally
                # this also makes samples poolable/stackable across a
                # hindcast window even for dates whose raw L length varies.
                if "L" in ds.dims:
                    ds = ds.sel(L=slice(None, 45))

                # Normalize Y (lat) to ascending (-90 -> 90) before anything else.
                # Different providers store hindcasts in different native orders
                # (e.g. CFSv2/GEFSv12_CPC are descending 90 -> -90, others are
                # already ascending) but this script stacks raw member arrays
                # into the output with no coordinate values, so
                # compute_exceedance has to *infer* lat labels later from the
                # forecast field's orientation alone. Without a consistent
                # source convention here, that inference silently mislabels
                # (mirrors north/south) any model whose native order differs
                # from what forecast.py normalizes to -- confirmed to have
                # caused exactly that for CFSv2/GEFSv12_CPC (2026-07-13).
                if "Y" in ds.dims:
                    ds = ds.sortby("Y")

                # Round the spatial grid to a fixed precision and collapse any
                # resulting duplicate rows. GMAO-GEOS_V3's raw hindcast archive
                # is inconsistent about whether its near-equator gridpoint
                # lands at exactly 0.0 or at floating-point noise like
                # -2.94e-13, and a handful of files carry both as separate
                # rows -- this produces a 182-point (vs. correct 181-point)
                # grid for those files, which then fails to np.stack against
                # every other file's array later in
                # _compute_and_write_one_mmdd ("all input arrays must have
                # the same shape"). Same fix as static/make_subx_climo.py's
                # _preproc_rt_root, applied here too since this script loads
                # hindcast files independently rather than sharing that code.
                for _dim in ("Y", "X"):
                    if _dim in ds.coords:
                        ds = ds.assign_coords({_dim: np.round(ds[_dim].values, 6)})
                if "Y" in ds.dims and not ds.indexes["Y"].is_unique:
                    ds = ds.groupby("Y").mean()
                if "X" in ds.dims and not ds.indexes["X"].is_unique:
                    ds = ds.groupby("X").mean()

                # Select pressure level when var has a P dimension (e.g. zg) --
                # do this before the all-NaN sample check below so the check
                # inspects the actually-requested level, not an arbitrary one.
                if _is_pressure_level(lev) and "P" in ds.dims:
                    ds = ds.sel(P=int(lev))

                # Cheap all-NaN placeholder check: some providers/models publish a
                # full-size NaN-filled file for calendar dates outside their valid
                # hindcast window (e.g. GEPS8 only has real data Jun 20-Dec 30).
                # A coarse, globally-strided sample (rather than reading the whole
                # ~40MB array via .values below) is enough to reliably detect this
                # while avoiding the wasted I/O of a full read for placeholder dates.
                sample = ds[var]
                for dim, size in sample.sizes.items():
                    if dim not in ("Y", "X") and size > 0:
                        sample = sample.isel({dim: 0})
                if "Y" in sample.dims and sample.sizes["Y"] > 1:
                    sample = sample.isel(Y=slice(0, None, max(1, sample.sizes["Y"] // 10)))
                if "X" in sample.dims and sample.sizes["X"] > 1:
                    sample = sample.isel(X=slice(0, None, max(1, sample.sizes["X"] // 10)))
                if bool(sample.isnull().all()):
                    ds.close()
                    n_missing += 1
                    continue

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

    # The 366 MM-DD outputs are fully independent (same read-only input pool,
    # separate output files), so fan them out across a process pool instead
    # of computing one at a time -- for models with large sample pools (e.g.
    # CFSv2, which folds 4 daily cycles x many years into the per-day pool)
    # the per-MM-DD stack/partition was the dominant cost (hours per model
    # run serially), and this is embarrassingly parallel.
    _WORKER_STATE.update(
        calendardates=calendardates,
        loaded_doy=loaded_doy,
        lead_coords=lead_coords,
        window=window,
        quantile=quantile,
        out_dir=out_dir,
        var=var,
        group=group,
        model=model,
        percentile=percentile,
        overwrite=overwrite,
        dry_run=dry_run,
    )

    counts = {"wrote": 0, "existed": 0, "no_data": 0, "dry_run": 0}
    print(f"\n[{model_id}] Computing {percentile}th percentile (window=±{window}d) "
          f"with {n_workers} worker process(es)...")
    ctx = mp.get_context("fork")
    with ProcessPoolExecutor(max_workers=n_workers, mp_context=ctx) as pool:
        futures = {pool.submit(_compute_and_write_one_mmdd, mmdd): mmdd for mmdd in all_target_mmdd}
        for fut in tqdm(as_completed(futures), total=len(futures), desc="MM-DD"):
            counts[fut.result()] += 1

    n_wrote = counts["wrote"] + counts["dry_run"]
    verb = "would write" if dry_run else "wrote"
    print(f"\n  {verb} {n_wrote} files, {counts['existed']} already existed (skipped)")
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
    ap.add_argument("--workers",    type=int, default=DEFAULT_WORKERS,
                    help=f"Parallel worker processes for the per-MM-DD percentile "
                         f"computation (default: {DEFAULT_WORKERS})")
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
        n_workers=args.workers,
    )


if __name__ == "__main__":
    raise SystemExit(main())
