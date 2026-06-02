#!/usr/bin/env python3
"""Generate daily SubX climatology files from hindcast ensemble-mean files.

Reads hindcast ensemble-mean files at:
  {hc_root}/{var}{lev}/daily/full/{group}-{model}/{var}_{lev}_{group}-{model}_YYYYMMDD.emean.daily.nc

Writes per-MMDD climatology files to:
  {hc_root}/{var}{lev}/daily/climo/{group}-{model}/{var}_{group}-{model}_{MMDD}.climo.p.nc

Example:
  python make_subx_climo.py --config ../config.yaml --group ECCC --model GEPS8 --var pr --lev sfc
"""

from __future__ import annotations

import argparse
import os
import tempfile
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr
import yaml

import warnings
warnings.filterwarnings("ignore")


def _load_hc_root(config_path: str) -> str:
    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    hc_root = (cfg.get("paths") or {}).get("hc_root", "")
    if not hc_root:
        raise RuntimeError("paths.hc_root not set in config.yaml")
    return hc_root


def _preproc(ds: xr.Dataset) -> xr.Dataset:
    """Rename time → lead and assign integer lead indices."""
    ds = ds.rename({"time": "lead"})
    ds["lead"] = np.arange(len(ds["lead"]))
    return ds


def _parse_date_from_filename(path: str) -> str | None:
    """Extract YYYYMMDD from filename like var_lev_GROUP-MODEL_YYYYMMDD.emean.daily.nc"""
    stem = os.path.basename(path)
    parts = stem.split("_")
    for part in reversed(parts):
        candidate = part.split(".")[0]
        if len(candidate) == 8 and candidate.isdigit():
            return candidate
    return None


def _triangular_smooth(da: xr.DataArray, window: int = 31, passes: int = 2) -> xr.DataArray:
    """Apply triangular smoothing via two rolling-mean passes with periodic padding."""
    result = da.copy()
    pad = window // 2
    for _ in range(passes):
        result = xr.concat(
            [result[-pad:], result, result[:pad]],
            dim="dayofyear",
        )
        result = result.rolling(dayofyear=window, center=True, min_periods=1).mean()
        result = result.isel(dayofyear=slice(pad, -pad))
    return result


def make_climo(
    hc_root: str,
    group: str,
    model: str,
    var: str,
    lev: str,
    start_year: int,
    end_year: int,
    overwrite: bool,
) -> None:
    in_dir = Path(hc_root) / f"{var}{lev}" / "daily" / "full" / f"{group}-{model}"
    out_dir = Path(hc_root) / f"{var}{lev}" / "daily" / "climo" / f"{group}-{model}"

    # Variable name inside the hindcast NetCDF files is {var}_{lev}
    var_in_file = f"{var}_{lev}"

    pattern = str(in_dir / f"{var}_{lev}_{group}-{model}_*.emean.daily.nc")
    import glob
    files = sorted(glob.glob(pattern))
    if not files:
        raise FileNotFoundError(f"No hindcast files found matching: {pattern}")
    print(f"Found {len(files)} hindcast files in {in_dir}")

    # Parse init dates from filenames
    dates = []
    valid_files = []
    for f in files:
        d = _parse_date_from_filename(f)
        if d:
            dates.append(d)
            valid_files.append(f)

    # Open all files, filter to requested year range
    ds = xr.open_mfdataset(
        valid_files,
        combine="nested",
        concat_dim="init",
        preprocess=_preproc,
    )
    ds["init"] = pd.to_datetime(dates)
    ds = ds.sel(init=slice(f"{start_year}-01-01", f"{end_year}-12-31"))
    print(f"Using {len(ds['init'])} init dates ({start_year}–{end_year})")

    # Compute daily climatology
    da = ds[var_in_file]
    print("Computing daily climatology (groupby dayofyear)...")
    da_clim = da.groupby("init.dayofyear").mean("init")

    # Pad to 366 days so smoothing wraps correctly
    all_doys = np.arange(1, 367)
    pad_da = xr.DataArray(
        np.full((366,) + da_clim.shape[1:], np.nan),
        coords=[all_doys, *[da_clim[d] for d in da_clim.dims[1:]]],
        dims=da_clim.dims,
    )
    da_clim = da_clim.combine_first(pad_da)

    # Apply triangular smoothing
    print("Applying triangular smoothing...")
    da_clim = _triangular_smooth(da_clim)

    # Rename variable and lead dimension for output
    da_clim.name = var
    ds_clim = da_clim.rename("lead" if "lead" in da_clim.dims else "lead").to_dataset(name=var)

    # Write one file per MMDD
    out_dir.mkdir(parents=True, exist_ok=True)
    doys = da_clim["dayofyear"].values
    print(f"Writing {len(doys)} per-MMDD files to {out_dir}")

    for doy in doys:
        mmdd = datetime.strptime(f"1960-{int(doy)}", "%Y-%j").strftime("%m%d")
        out_path = out_dir / f"{var}_{group}-{model}_{mmdd}.climo.p.nc"

        if out_path.exists() and not overwrite:
            continue

        day_ds = ds_clim.sel(dayofyear=doy)
        # Rename lead → time to match what forecast.py expects
        if "lead" in day_ds.dims:
            day_ds = day_ds.rename({"lead": "time"})

        tmp = out_path.with_suffix(".tmp.nc")
        day_ds.to_netcdf(tmp)
        tmp.rename(out_path)

    print(f"Done: {group}-{model} {var}{lev}")


def main() -> int:
    p = argparse.ArgumentParser(description="Generate daily SubX climatology files")
    p.add_argument("--config", default="../config.yaml", help="Path to config.yaml")
    p.add_argument("--group", required=True, help="Model group (e.g. ECCC)")
    p.add_argument("--model", required=True, help="Model name (e.g. GEPS8)")
    p.add_argument("--var",   required=True, help="Variable (e.g. pr, tas, zg)")
    p.add_argument("--lev",   required=True, help="Level suffix (e.g. sfc, 2m, 500, 200)")
    p.add_argument("--start-year", type=int, default=1999)
    p.add_argument("--end-year",   type=int, default=2015)
    p.add_argument("--overwrite", action="store_true", help="Overwrite existing files")
    args = p.parse_args()

    hc_root = _load_hc_root(args.config)
    make_climo(
        hc_root=hc_root,
        group=args.group,
        model=args.model,
        var=args.var,
        lev=args.lev,
        start_year=args.start_year,
        end_year=args.end_year,
        overwrite=args.overwrite,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
