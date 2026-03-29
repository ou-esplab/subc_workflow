#!/usr/bin/env python3
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
- resolve_threshold_path (template vs. GEFS fallback)
- compute_exceedance (generic thresholds)
- plot_exceedance_summary, plot_exceedance_panels (Cartopy plots)
"""
from __future__ import annotations
from typing import List, Tuple, Optional, Dict

import os
import json
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import xarray as xr

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

# ---------------------- Threshold path ---------------------- #

def resolve_threshold_path(
    template: str, percentile: int, modelkey: str, var: str,
    gefs_fallback: Optional[str]
) -> Optional[str]:
    """
    Resolve a threshold NetCDF path using a generic template, else fall back to GEFS path.

    template:
      e.g., '/.../percentile{percentile}_{modelkey}_thres_{var}.nc'
    gefs_fallback:
      e.g., '/.../percentile{percentile}_GEFSthres_pr.nc'
    """
    if template:
        path = template.format(percentile=percentile, modelkey=modelkey, var=var)
        if os.path.exists(path):
            return path
    if gefs_fallback:
        fb = gefs_fallback.format(percentile=percentile)
        if os.path.exists(fb):
            return fb
    return None

# ---------------------- Exceedance ---------------------- #

def compute_exceedance(
    ds_field: xr.DataArray,
    threshold_path: str,
    init_date: pd.Timestamp,
    window: int = 7,
    percentile: int = 95,
    var: str = 'pr',
    lon_slice: Optional[Tuple[float, float]] = None,
    lat_slice: Optional[Tuple[float, float]] = None,
    return_counts: bool = False,
) -> xr.DataArray:
    """
    Probability (or counts) that within each rolling window along 'lead' at least one day
    exceeds the percentile threshold.

    Restores the original vectorized approach:
      1) Subset forecast to region.
      2) Select threshold for month_day near init_date and align to forecast lon/lat.
      3) Align threshold lead length to forecast lead length and assign forecast 'lead' coords.
      4) Vectorized rolling window comparison on both arrays.
      5) Drop first (window-1) incomplete windows.
      6) Aggregate over ensemble 'M' if present (counts or mean*100).

    Notes
    -----
    - Works whether forecast 'lead' is datetime64 and threshold 'lead' is integer indices.
    - Avoids label-based selection on 'lead' entirely.
    - Scrubs attrs on intermediates to prevent NetCDF attr dtype issues later.
    """
    # --------- 0) Defensive checks / copy ----------
    field = ds_field.copy()
    field = field.assign_attrs({})  # scrub attrs to avoid netCDF attr issues

    # --------- 1) Units and regional subset ----------
    if var == 'pr' or (hasattr(field, 'name') and field.name == 'pr'):
        # kg m^-2 s^-1 -> mm/day
        field = field * 86400.0

    if lon_slice:
        field = field.sel(lon=slice(*lon_slice))
    if lat_slice:
        field = field.sel(lat=slice(*lat_slice))

    # Must have 'lead'
    if 'lead' not in field.sizes:
        raise ValueError(f"compute_exceedance: forecast has no 'lead' dimension. dims={dict(field.sizes)}")

    n_lead = int(field.sizes['lead'])
    if n_lead < 1:
        raise ValueError("compute_exceedance: forecast lead length is zero.")

    # --------- 2) Load thresholds and align lon/lat ----------
    thr_path = threshold_path.format(percentile=percentile) if '{percentile' in threshold_path else threshold_path
    thr = xr.open_dataset(thr_path)
    thr = thr.assign_attrs({})
    for vv in thr.data_vars:
        thr[vv].attrs = {}

    # Normalize dim names if needed
    if 'L' in thr.dims:
        thr = thr.rename({'L': 'lead'})

    # Align threshold lon/lat to forecast grid (nearest)
    if 'lon' in thr.coords and 'lat' in thr.coords:
        thr = thr.sel(lon=field.lon, lat=field.lat, method='nearest')

    # --------- 3) Select month_day near init_date and align 'lead' length/coords ----------
    init_mdy = pd.Timestamp(init_date).strftime('%m-%d')
    if 'month_day' in thr.coords:
        if init_mdy in thr['month_day'].values:
            thr_day = thr.sel(month_day=init_mdy)
        else:
            thr_day = thr.sel(month_day=init_mdy, method='nearest')
    else:
        # Some threshold files may omit month_day; assume already on correct slice
        thr_day = thr

    if var not in thr_day.data_vars:
        raise KeyError(f"compute_exceedance: variable '{var}' not found in threshold file {thr_path}")

    thr_var = thr_day[var]

    # Align lead lengths with forecast
    if 'lead' not in thr_var.dims:
        raise ValueError("compute_exceedance: thresholds have no 'lead' dimension.")
    n_lead_thr = int(thr_var.sizes['lead'])

    # Use min across forecast and threshold, then assign forecast 'lead' coordinates
    n_common = min(n_lead, n_lead_thr)
    if n_common <= 0:
        raise ValueError(f"compute_exceedance: no common lead length (field={n_lead}, threshold={n_lead_thr}).")

    field = field.isel(lead=slice(0, n_common))
    thr_var = thr_var.isel(lead=slice(0, n_common))
    # Broadcast threshold to forecast's lon/lat/lead coords (already matched lon/lat above)
    thr_var = thr_var.assign_coords(lead=field.lead)

    # --------- 4) Rolling window comparison (vectorized) ----------
    if window < 1:
        raise ValueError(f"compute_exceedance: window must be >=1, got {window}")
    if n_common < window:
        # Not enough lead to form even one window: return empty (0 windows)
        empty = xr.full_like(field.isel(lead=0, drop=True), np.nan).expand_dims(lead=0)
        # We’ll expose this as 0 windows by slicing below; keep shape consistent
        return empty.rename(field.name if field.name else var).drop_vars(
            [c for c in empty.coords if c not in ('lat', 'lon')], errors='ignore'
        )

    rolled_fcst   = field.rolling(lead=window).construct("window_dim")
    rolled_thresh = thr_var.rolling(lead=window).construct("window_dim")

    # Compare within window: any day exceeds threshold → True for that window end
    exceed_bool = (rolled_fcst > rolled_thresh).any(dim="window_dim")

    # Drop the first (window-1) incomplete windows
    exceed_bool = exceed_bool.isel(lead=slice(window - 1, None))

    # --------- 5) Aggregate across ensemble if present ----------
    # Support either 'M' or 'member' as ensemble dimension
    ens_dim = 'M' if 'M' in exceed_bool.dims else ('member' if 'member' in exceed_bool.dims else None)

    if ens_dim:
        if return_counts:
            out = exceed_bool.sum(dim=ens_dim)  # counts of members with at-least-one-day exceedance
        else:
            out = exceed_bool.mean(dim=ens_dim) * 100.0  # probability (%)
        # Optionally add nens as coordinate
        try:
            nens = int(exceed_bool.sizes[ens_dim])
            out = out.assign_coords(nens=nens)
        except Exception:
            pass
    else:
        # Deterministic field: cast True/False to 100/0 (or keep bool if you prefer)
        out = exceed_bool.astype(float) * 100.0

    # --------- 6) Tidy output (name, coords, attrs) ----------
    # Rename 'lead' to 'time_window' so downstream code can do mean('time_window') etc.
    out = out.rename({'lead': 'time_window'})
    out.name = f"{var}_exceed_prob"

    # Scrub any lingering attrs on coords/var to keep NetCDF happy later
    out.attrs = {}
    for c in out.coords:
        try:
            out[c].attrs = {}
        except Exception:
            pass

    return out
# ---------------------- Plotting ---------------------- #
def plot_exceedance_summary(probs, title, out_png, cmap='plasma'):
    """
    Plot mean across time_window for exceedance probabilities.
    probs: xr.DataArray with dims including 'time_window','lat','lon'
    """
    import matplotlib.pyplot as plt
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature

    fig, ax = plt.subplots(figsize=(8, 4), subplot_kw={'projection': ccrs.PlateCarree()})
    data = probs.mean('time_window')
    data.plot(ax=ax, transform=ccrs.PlateCarree(), cmap=cmap,
              vmin=0, vmax=100, cbar_kwargs={'label':'%','shrink':0.8})
    ax.coastlines('110m', linewidth=1)
    ax.add_feature(cfeature.BORDERS, linewidth=0.6)
    ax.add_feature(cfeature.STATES,  linewidth=0.4)
    ax.set_title(title, fontsize=10)
    plt.tight_layout()
    plt.savefig(out_png, dpi=150, bbox_inches='tight')
    plt.close(fig)


def plot_exceedance_panels(probs, init_date, window, out_png_prefix, cmap='plasma'):
    """
    Plot one PNG per time_window step.
    Writes: <out_png_prefix>_stepNN.png
    """
    import matplotlib.pyplot as plt
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature

    for i in range(probs.sizes['time_window']):
        fig, ax = plt.subplots(figsize=(8, 4), subplot_kw={'projection': ccrs.PlateCarree()})
        data = probs.isel(time_window=i)
        data.plot(ax=ax, transform=ccrs.PlateCarree(), cmap=cmap,
                  vmin=0, vmax=100, cbar_kwargs={'label':'%','shrink':0.8})
        ax.coastlines('110m', linewidth=1)
        ax.add_feature(cfeature.BORDERS, linewidth=0.6)
        ax.add_feature(cfeature.STATES,  linewidth=0.4)
        ax.set_title(f"Exceedance Prob – step {i+1} – init {str(init_date)[:10]} – {window}d", fontsize=10)
        plt.tight_layout()
        out_png = f"{out_png_prefix}_step{str(i+1).zfill(2)}.png"
        plt.savefig(out_png, dpi=150, bbox_inches='tight')
        plt.close(fig)


__all__ = [
    'latest_thursday', 'fcst_week_dates', 'ensure_lon', 'safe_concat',
    'weekly_reduce', 'save_manifest', 'resolve_threshold_path',
    'compute_exceedance',
    'plot_exceedance_summary', 'plot_exceedance_panels',
]