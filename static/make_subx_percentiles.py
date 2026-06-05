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


DEFAULT_YEARS = list(range(1999, 2016))


def _load_cfg(config_path: str) -> dict:
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _parse_model(model_id: str) -> tuple[str, str]:
    if "-" not in model_id:
        raise ValueError(f"--model must be GROUP-MODEL format (e.g. ECCC-GEPS8), got: {model_id!r}")
    group, model = model_id.split("-", 1)
    return group, model


def _is_pressure_level(lev: str) -> bool:
    try:
        int(lev)
        return True
    except ValueError:
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
    calendardates: dict[str, list[xr.Dataset]] = {}
    n_loaded = 0
    n_missing = 0
    n_skipped = 0

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

                ds = xr.open_dataset(path, chunks="auto")

                # Select pressure level when var has a P dimension (e.g. zg)
                if _is_pressure_level(lev) and "P" in ds.dims:
                    ds = ds.sel(P=int(lev))

                # Skip entirely-NaN files
                if ds[var].isnull().all().compute().item():
                    ds.close()
                    n_skipped += 1
                    continue

                mmdd = ts.strftime("%m-%d")
                calendardates.setdefault(mmdd, []).append(ds)
                n_loaded += 1

    print(f"  loaded={n_loaded}  missing={n_missing}  skipped_nan={n_skipped}")
    print(f"  grouped into {len(calendardates)} calendar days")

    if not calendardates:
        print("[ERROR] No data loaded — aborting.")
        return 1

    # ── 2. Compute percentile per MM-DD and write output ──────────────────────
    n_wrote = 0
    n_existed = 0

    print(f"\n[{model_id}] Computing {percentile}th percentile and writing output...")
    for mmdd in tqdm(sorted(calendardates.keys()), desc="MM-DD"):
        mmdd_str = mmdd.replace("-", "")
        out_file = out_dir / f"{var}_{group}-{model}_{mmdd_str}.{percentile}p.nc"

        if out_file.exists() and not overwrite:
            n_existed += 1
            continue

        if dry_run:
            print(f"  [DRY-RUN] {out_file}")
            n_wrote += 1
            continue

        dslist = calendardates[mmdd]

        # Stack all years and ensemble members into a single 'stacked' dim
        combined = xr.concat(dslist, dim="year")
        if "M" in combined.dims:
            combined = combined.stack(stacked=("year", "M"))
        else:
            combined = combined.rename({"year": "stacked"})
        combined = combined.reset_index("stacked").compute()

        n_leads = combined.sizes["L"]
        lead_arrays = []

        for i in range(n_leads):
            var_data = combined.isel(L=i)[var]
            # Drop S coordinate/dim left over from SubX file convention
            if "S" in var_data.dims:
                var_data = var_data.isel(S=0, drop=True)
            elif "S" in var_data.coords:
                var_data = var_data.drop_vars("S")

            pct_da = (
                var_data
                .quantile(quantile, dim="stacked", skipna=True)
                .drop_vars("quantile")
            )
            lead_arrays.append(pct_da)

        # Assemble output: dims (month_day: 1, L, Y, X)
        da = xr.concat(lead_arrays, dim="L").assign_coords(L=list(range(n_leads)))
        da = da.expand_dims(month_day=[mmdd])
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
        years=years,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    raise SystemExit(main())
