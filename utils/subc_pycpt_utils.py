#!/usr/bin/env python3
from __future__ import annotations
# coding: utf-8
"""
subc_pycpt_utils.py
--------------------
Reusable utilities shared across the SubX + PyCPT pipeline.

Functions
---------
- latest_thursday, fcst_week_dates
- ensure_lon (lon convention normalization)
- safe_concat (robust xarray concat)
- weekly_reduce (CPC Sat–Fri windows)
- save_manifest (JSON)

Exceedance/threshold helpers (compute_exceedance, plot_exceedance_summary,
plot_exceedance_panels) live in utils/exceedance_utils.py, not here.
"""

import xarray as xr
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Optional, Dict

import os
import json
import numpy as np

def build_local_chirps_weekly(
    daily_dir: Path,
    out_dir: Path,
    varname: str,
    fcst_dt: datetime,
    leads,
    lat_range,
    lon_range,
):
    """
    Build weekly CHIRPS files from daily data for each lead window.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    for lead_name, lead_low, lead_high in leads:
        start = fcst_dt + timedelta(days=lead_low - 1)
        end = fcst_dt + timedelta(days=lead_high - 1)
        # Find all daily files in the range
        days = pd.date_range(start, end)
        daily_files = [daily_dir / f"chirps-v2.0.{d.strftime('%Y.%m.%d')}.nc" for d in days]
        if not all(f.exists() for f in daily_files):
            print(f"[WARN] Missing daily CHIRPS files for {lead_name}: {[str(f) for f in daily_files if not f.exists()]}")
            continue
        # Open and stack
        ds_list = [xr.open_dataset(f)[varname] for f in daily_files]
        arr = xr.concat(ds_list, dim="time")
        # Subset
        arr = arr.sel(lat=slice(*lat_range), lon=slice(*lon_range))
        # Aggregate to weekly sum
        arr_week = arr.sum(dim="time")
        arr_week = arr_week.expand_dims(time=[start])
        # Save in expected format
        out_name = f"CHIRPS.PRCP-{lead_low}-{lead_high}.nc"
        arr_week.to_netcdf(out_dir / out_name)


# ---------------------- Date helpers ---------------------- #

def latest_thursday(date_str: Optional[str] = None) -> str:
    """Return YYYYMMDD for the most recent Thursday (UTC)."""
    if date_str:
        d = datetime.strptime(date_str, "%Y%m%d")
    else:
        d = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return (d - timedelta(days=(d.weekday() - 3) % 7)).strftime("%Y%m%d")


def fcst_week_dates(fcstdate: str) -> pd.DatetimeIndex:
    """Forecast selection window is Fri–Thu ending on fcstdate (Thu)."""
    d = datetime.strptime(fcstdate, "%Y%m%d")
    start = d - timedelta(days=6)
    return pd.date_range(start=start, end=d, freq="D")

# ---------------------- xarray helpers ---------------------- #

def ensure_lon(ds: xr.Dataset, convention: str) -> xr.Dataset:
    """Normalize longitudes to 0..360 ('0_360') or −180..180 ('negpos')."""
    if 'lon' not in ds.coords:
        return ds
    lon = ds['lon']
    if convention == '0_360':
        lon = xr.where(lon < 0, lon + 360, lon)
    elif convention == 'negpos':
        lon = xr.where(lon > 180, lon - 360, lon)
    return ds.assign_coords(lon=lon).sortby('lon')


def safe_concat(dsets: List[xr.Dataset], dim: str = 'model') -> xr.Dataset:
    """Pad missing variables with NaN and concat across heterogeneous models."""
    all_vars = set().union(*(ds.data_vars for ds in dsets))
    fixed = []
    for ds in dsets:
        ds = ds.copy()
        for v in all_vars:
            if v not in ds:
                tpl = next(d[v] for d in dsets if v in d)
                ds[v] = xr.full_like(tpl, np.nan)
        fixed.append(ds)
    return xr.concat(fixed, dim=dim, coords='minimal', compat='override')


def weekly_reduce(ds: xr.Dataset, fcstdate: str, nweeks: int = 4) -> xr.Dataset:
    """
    Aggregate to CPC Sat–Fri weeks starting 2 days after the Thursday label.
    - Precip variables named 'pr*' are summed (kg/m2/s → mm/day first)
    - Others are averaged
    """
    start = datetime.strptime(fcstdate, "%Y%m%d") + timedelta(days=2)  # Saturday
    weeks = []
    meta  = {'start': [], 'end': []}
    for i in range(nweeks):
        w0 = start + timedelta(days=7 * i)
        w1 = w0 + timedelta(days=6)
        out_vars = []
        for v in ds.data_vars:
            slab = ds[v].sel(lead=slice(w0, w1))
            if v.startswith('pr'):
                out_vars.append((slab * 86400.0).sum('lead'))
            else:
                out_vars.append(slab.mean('lead'))
        weeks.append(xr.merge(out_vars))
        meta['start'].append(np.datetime64(w0))
        meta['end'].append(np.datetime64(w1))
    out = xr.combine_nested(weeks, concat_dim='week')
    out['week'] = np.arange(1, nweeks + 1)
    out.attrs['week_start'] = meta['start']
    out.attrs['week_end']   = meta['end']
    return out

# ---------------------- I/O helpers ---------------------- #

def save_manifest(path: str, data: Dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        json.dump(data, f, indent=2, default=str)

__all__ = [
    'latest_thursday', 'fcst_week_dates', 'ensure_lon', 'safe_concat',
    'weekly_reduce', 'save_manifest',
]
