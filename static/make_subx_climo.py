#!/usr/bin/env python3
"""Generate daily SubX climatology files from rt_root hindcast data.

Reads hindcast files from rt_root (the master archive):
  {rt_root}/{group}-{model}/hindcast/{var}/{var}_{group}-{model}_YYYYMMDD.daily.nc
  dims: (S: 1, M: N, L: N_leads, Y: 181, X: 360)

Writes per-MMDD climatology files to:
  {hc_root}/{var}{lev}/daily/climo/{group}-{model}/{var}_{group}-{model}_{MMDD}.climo.p.nc

Examples
--------
  # Single model/var
  python make_subx_climo.py --config ../config.yaml \\
      --group ECCC --model GEPS8 --var pr --lev sfc

  # All models and variables from config
  python make_subx_climo.py --config ../config.yaml --all --overwrite
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import tempfile
import warnings
from datetime import datetime
from functools import partial
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr
import yaml

warnings.filterwarnings("ignore")


# ── Config helpers ────────────────────────────────────────────────────────────

def _load_cfg(config_path: str) -> dict:
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_paths(config_path: str) -> tuple[str, str]:
    cfg = _load_cfg(config_path)
    paths = cfg.get("paths") or {}
    rt_root = paths.get("rt_root", "")
    hc_root = paths.get("hc_root", "")
    if not rt_root:
        raise RuntimeError("paths.rt_root not set in config.yaml")
    if not hc_root:
        raise RuntimeError("paths.hc_root not set in config.yaml")
    return rt_root, hc_root


# ── rt_root file loading ──────────────────────────────────────────────────────

def _parse_date_from_filename(path: str) -> str | None:
    """Extract YYYYMMDD from filename like var_GROUP-MODEL_YYYYMMDD.daily.nc"""
    stem = os.path.basename(path)
    m = re.search(r'(\d{8})\.daily\.nc$', stem)
    return m.group(1) if m else None


def _preproc_rt_root(ds: xr.Dataset, var: str, lev: str) -> xr.Dataset:
    """Preprocess one rt_root hindcast file: collapse S, select P, mean over M, normalise L."""
    # Collapse S dimension: drop if scalar, otherwise average (e.g. CFSv2 has S=5 sub-daily inits)
    if "S" in ds.dims:
        if ds.sizes["S"] == 1:
            ds = ds.isel(S=0, drop=True)
        else:
            ds = ds.mean(dim="S")
    # Select pressure level for multi-level variables (e.g. zg at 500 hPa)
    try:
        plev = int(lev)
        if "P" in ds.dims:
            ds = ds.sel(P=plev, drop=True)
    except ValueError:
        pass
    # Average over ensemble members
    if "M" in ds.dims:
        ds = ds.mean(dim="M")
    # Rename L → time and convert to integer lead indices
    if "L" in ds.dims:
        ds = ds.rename({"L": "time"})
        ds["time"] = np.arange(len(ds["time"]))
    # Rename Y/X → lat/lon to match what forecast.py expects after its own rename
    rename = {k: v for k, v in {"Y": "lat", "X": "lon"}.items() if k in ds.dims}
    if rename:
        ds = ds.rename(rename)
    return ds


def _is_all_nan(path: str, var: str, lev: str) -> bool:
    """Fast NaN check: sample a center spatial crop to avoid masked polar/coastal points."""
    try:
        ds = xr.open_dataset(path)
        da = ds[var]
        try:
            plev = int(lev)
            if "P" in da.dims:
                da = da.sel(P=plev)
        except ValueError:
            pass
        # Collapse S and M to get (L, Y, X)
        if "S" in da.dims:
            da = da.isel(S=0) if da.sizes["S"] == 1 else da.mean("S")
        if "M" in da.dims:
            da = da.isel(M=0)
        # Sample a 5×5 center crop at first lead — avoids polar NaN mask
        ny, nx = da.sizes.get("Y", 1), da.sizes.get("X", 1)
        cy, cx = ny // 2, nx // 2
        sample = da.isel(
            L=0 if "L" in da.dims else {},
            Y=slice(cy - 2, cy + 3),
            X=slice(cx - 2, cx + 3),
        )
        result = bool(sample.isnull().all().item())
        ds.close()
        return result
    except Exception:
        return True


def _open_full_hindcast(
    hindcast_dir: str,
    var: str,
    lev: str,
    start_year: int | None,
    end_year: int | None,
) -> tuple[xr.Dataset, list[str]]:
    """Open full/ IRI hindcast emean files (alternative to rt_root for models
    whose rt_root data is unusable, e.g. CFSv2 tas which is sea-ice only there).

    Files are named like: {var_prefix}_{GROUP}-{MODEL}_{YYYYMMDD}.emean.daily.nc
    Variable inside: {var} (same as requested, despite the filename prefix).
    Dims: (time: N_leads, lat, lon) with time as datetime starting 1960-01-01.
    """
    all_files = sorted(glob.glob(os.path.join(hindcast_dir, "*.emean.daily.nc")))
    if not all_files:
        raise FileNotFoundError(f"No *.emean.daily.nc files found in {hindcast_dir}")

    date_re = re.compile(r'_(\d{8})\.emean\.daily\.nc$')
    dated = [(f, m.group(1)) for f in all_files if (m := date_re.search(f))]

    years_all = [int(d[:4]) for _, d in dated]
    sy = start_year if start_year is not None else min(years_all)
    ey = end_year   if end_year   is not None else max(years_all)
    dated = [(f, d) for f, d in dated if sy <= int(d[:4]) <= ey]
    if not dated:
        raise FileNotFoundError(f"No emean files in {hindcast_dir} for years {sy}–{ey}")

    valid_files, dates = zip(*dated)
    print(f"  Loading {len(valid_files)} emean files ({sy}–{ey}) from {hindcast_dir}...")

    def _preproc(ds: xr.Dataset) -> xr.Dataset:
        # Convert datetime lead coord to integer indices so concat is clean
        ds["time"] = np.arange(len(ds["time"]))
        return ds

    ds = xr.open_mfdataset(
        list(valid_files),
        combine="nested",
        concat_dim="init",
        preprocess=_preproc,
        parallel=False,
    )
    ds["init"] = pd.to_datetime(list(dates))
    return ds, list(dates)


def _open_rt_root_hindcast(
    rt_root: str,
    group: str,
    model: str,
    var: str,
    lev: str,
    start_year: int | None,
    end_year: int | None,
) -> tuple[xr.Dataset, list[str]]:
    """Open all valid rt_root hindcast files as a multi-init Dataset.

    Returns (ds, dates) where ds has an 'init' dimension and dates is the
    list of YYYYMMDD strings corresponding to each init.
    """
    pattern = os.path.join(
        rt_root, f"{group}-{model}", "hindcast", var,
        f"{var}_{group}-{model}_*.daily.nc"
    )
    all_files = sorted(glob.glob(pattern))
    if not all_files:
        raise FileNotFoundError(f"No hindcast files found: {pattern}")

    # Filter to year range (auto-detect if not specified)
    dated = [(f, _parse_date_from_filename(f)) for f in all_files]
    dated = [(f, d) for f, d in dated if d]
    years = [int(d[:4]) for _, d in dated]
    if start_year is None:
        start_year = min(years)
    if end_year is None:
        end_year = max(years)

    dated = [(f, d) for f, d in dated if start_year <= int(d[:4]) <= end_year]
    if not dated:
        raise FileNotFoundError(
            f"No hindcast files for {group}-{model} {var} in {start_year}–{end_year}"
        )

    # Exclude all-NaN placeholder files
    print(f"  Checking {len(dated)} files for all-NaN placeholders...")
    valid = [(f, d) for f, d in dated if not _is_all_nan(f, var, lev)]
    n_skipped = len(dated) - len(valid)
    if n_skipped:
        print(f"  Skipped {n_skipped} all-NaN placeholder files")
    if not valid:
        raise ValueError(f"All hindcast files for {group}-{model} {var} are NaN")

    valid_files, dates = zip(*valid)
    print(f"  Loading {len(valid_files)} files ({start_year}–{end_year})...")

    preproc = partial(_preproc_rt_root, var=var, lev=lev)
    ds = xr.open_mfdataset(
        list(valid_files),
        combine="nested",
        concat_dim="init",
        preprocess=preproc,
        parallel=False,
    )
    ds["init"] = pd.to_datetime(list(dates))
    return ds, list(dates)


# ── Smoothing ─────────────────────────────────────────────────────────────────

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


# ── Core computation ──────────────────────────────────────────────────────────

def make_climo(
    rt_root: str,
    hc_root: str,
    group: str,
    model: str,
    var: str,
    lev: str,
    start_year: int | None,
    end_year: int | None,
    overwrite: bool,
    hindcast_dir: str | None = None,
) -> None:
    model_id = f"{group}-{model}"
    out_dir = Path(hc_root) / f"{var}{lev}" / "daily" / "climo" / model_id
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n[{model_id}] {var}{lev}")

    if hindcast_dir:
        ds, dates = _open_full_hindcast(hindcast_dir, var, lev, start_year, end_year)
    else:
        ds, dates = _open_rt_root_hindcast(rt_root, group, model, var, lev, start_year, end_year)
    actual_start = min(int(d[:4]) for d in dates)
    actual_end   = max(int(d[:4]) for d in dates)
    print(f"  Using {len(dates)} init dates ({actual_start}–{actual_end})")

    da = ds[var]

    # Compute daily climatology grouped by day-of-year
    print("  Computing daily climatology (groupby dayofyear)...")
    da_clim = da.groupby("init.dayofyear").mean("init").compute()

    # Pad to 366 days so smoothing wraps correctly at year boundaries
    all_doys = np.arange(1, 367)
    pad_da = xr.DataArray(
        np.full((366,) + da_clim.shape[1:], np.nan),
        coords=[all_doys, *[da_clim[d] for d in da_clim.dims[1:]]],
        dims=da_clim.dims,
    )
    da_clim = da_clim.combine_first(pad_da)

    # Apply triangular smoothing
    print("  Applying triangular smoothing...")
    da_clim = _triangular_smooth(da_clim)
    da_clim.name = var

    # Write one file per MMDD
    doys = da_clim["dayofyear"].values
    n_wrote = n_skipped = 0
    print(f"  Writing {len(doys)} per-MMDD files to {out_dir}")

    for doy in doys:
        mmdd = datetime.strptime(f"1960-{int(doy)}", "%Y-%j").strftime("%m%d")
        out_path = out_dir / f"{var}_{group}-{model}_{mmdd}.climo.p.nc"

        if out_path.exists() and not overwrite:
            n_skipped += 1
            continue

        day_da = da_clim.sel(dayofyear=doy).drop_vars("dayofyear")
        # forecast.py reads climo and renames lead→time, so write with 'time' dim
        if "time" not in day_da.dims and "lead" in day_da.dims:
            day_da = day_da.rename({"lead": "time"})
        day_ds = day_da.to_dataset(name=var)

        tmp = out_path.with_suffix(".tmp.nc")
        day_ds.to_netcdf(tmp)
        tmp.rename(out_path)
        n_wrote += 1

    print(f"  Done: wrote={n_wrote}  skipped={n_skipped}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Generate per-MMDD SubX climatology files from rt_root hindcast data"
    )
    ap.add_argument("--config", default="../config.yaml", help="Path to config.yaml")
    ap.add_argument("--all", action="store_true",
                    help="Process all models and variables from config (overrides --group/--model/--var/--lev)")
    ap.add_argument("--group",  help="Model group (e.g. ECCC)")
    ap.add_argument("--model",  help="Model name (e.g. GEPS8)")
    ap.add_argument("--var",    help="Variable (e.g. pr, tas, zg)")
    ap.add_argument("--lev",    help="Level suffix (e.g. sfc, 2m, 500, 200)")
    ap.add_argument("--start-year", type=int, default=None,
                    help="First hindcast year (default: auto-detect from files)")
    ap.add_argument("--end-year",   type=int, default=None,
                    help="Last hindcast year (default: auto-detect from files)")
    ap.add_argument("--hindcast-dir", default=None,
                    help="Read from this directory of full/ IRI emean files instead of rt_root "
                         "(e.g. for CFSv2 tas whose rt_root data is sea-ice only)")
    ap.add_argument("--overwrite", action="store_true", help="Overwrite existing files")
    args = ap.parse_args()

    cfg = _load_cfg(args.config)
    try:
        rt_root, hc_root = _load_paths(args.config)
    except RuntimeError as e:
        print(f"[ERROR] {e}")
        return 1

    if args.all:
        models = cfg.get("models", [])
        if not models:
            print("[ERROR] No models defined in config.yaml")
            return 1
        errors = []
        for m_cfg in models:
            group = m_cfg["group"]
            model = m_cfg["name"]
            for var, lev in zip(m_cfg["vars"], m_cfg["levels"]):
                try:
                    make_climo(
                        rt_root=rt_root,
                        hc_root=hc_root,
                        group=group,
                        model=model,
                        var=var,
                        lev=lev,
                        start_year=args.start_year,
                        end_year=args.end_year,
                        overwrite=args.overwrite,
                        hindcast_dir=args.hindcast_dir,
                    )
                except Exception as exc:
                    msg = f"[ERROR] {group}-{model} {var}{lev}: {exc}"
                    print(msg)
                    errors.append(msg)
        if errors:
            print(f"\n{len(errors)} error(s) encountered:")
            for e in errors:
                print(f"  {e}")
            return 1
        return 0

    # Single model/var run
    missing = [n for n, v in [("--group", args.group), ("--model", args.model),
                               ("--var", args.var), ("--lev", args.lev)] if not v]
    if missing:
        print(f"[ERROR] Required when not using --all: {', '.join(missing)}")
        return 1

    try:
        make_climo(
            rt_root=rt_root,
            hc_root=hc_root,
            group=args.group,
            model=args.model,
            var=args.var,
            lev=args.lev,
            start_year=args.start_year,
            end_year=args.end_year,
            overwrite=args.overwrite,
            hindcast_dir=args.hindcast_dir,
        )
    except Exception as exc:
        print(f"[ERROR] {exc}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
