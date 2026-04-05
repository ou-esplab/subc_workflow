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
- resolve_threshold_path (template vs. GEFS fallback)
- compute_exceedance (generic thresholds)
- plot_exceedance_summary, plot_exceedance_panels (Cartopy plots)
"""

import xarray as xr
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Tuple, Optional, Dict

import os
import glob
import json
import re
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


def _nearest_month_day_label(labels, init_mdy: str) -> str:
    """Return exact MM-DD label if present, otherwise nearest by circular day-of-year distance."""
    label_strs = [str(x) for x in labels]
    if init_mdy in label_strs:
        return init_mdy

    target = datetime.strptime(init_mdy, "%m-%d")

    def score(lbl: str) -> int:
        try:
            d = datetime.strptime(lbl, "%m-%d")
            diff = abs((d - target).days)
            return min(diff, 366 - diff)
        except ValueError:
            return 10**9

    return min(label_strs, key=score)


def nearest_mmdd_threshold(
    thr_dir: str,
    var: str,
    group: str,
    model: str,
    mmdd: str,
    pct: int,
    max_offset_days: int = 7,
) -> str | None:
    """Return exact MMDD threshold file if present, else nearest MMDD file within max_offset_days.

    Canonical filename form:
    - {var}_{group}-{model}_{MMDD}.{pct}p.nc
    """
    candidates = sorted(glob.glob(os.path.join(thr_dir, f"{var}_{group}-{model}_*.{pct}p.nc")))
    if not candidates:
        return None

    mmdd_re = re.compile(
        rf"^{re.escape(var)}_{re.escape(group)}-{re.escape(model)}_(\d{{4}})\.{pct}p\.nc$"
    )

    parsed: list[tuple[str, str]] = []
    for path in candidates:
        stem = os.path.basename(path)
        match = mmdd_re.match(stem)
        if not match:
            continue
        parsed.append((path, match.group(1)))

    if not parsed:
        return None

    for path, mmdd_token in parsed:
        if mmdd_token == mmdd:
            return path

    target = datetime.strptime(mmdd, "%m%d")

    def score(mmdd_token: str) -> int:
        d = datetime.strptime(mmdd_token, "%m%d")
        diff = abs((d - target).days)
        return min(diff, 366 - diff)

    best_path, best_token = min(parsed, key=lambda rec: score(rec[1]))
    best_offset = score(best_token)
    if best_offset > max_offset_days:
        return None
    return best_path

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

    # --------- 1) Regional subset ----------
    # Do NOT convert units here: threshold files are in the same native units
    # as the model output (kg m^-2 s^-1); converting only the forecast would
    # cause a systematic unit mismatch and make everything exceed the threshold.

    def _ordered_slice(arr: xr.DataArray, dim: str, bounds: Tuple[float, float]) -> xr.DataArray:
        lo, hi = bounds
        coord = arr[dim]
        if coord.size == 0:
            return arr
        first = float(coord.values[0])
        last = float(coord.values[-1])
        if first <= last:
            return arr.sel({dim: slice(lo, hi)})
        return arr.sel({dim: slice(hi, lo)})

    if lon_slice:
        field = _ordered_slice(field, 'lon', lon_slice)
    if lat_slice:
        field = _ordered_slice(field, 'lat', lat_slice)

    for dim in ('lat', 'lon'):
        if dim in field.sizes and int(field.sizes[dim]) == 0:
            raise ValueError(
                f"compute_exceedance: empty regional subset after {dim} selection; "
                f"lon_slice={lon_slice}, lat_slice={lat_slice}, "
                f"coord_range=({float(ds_field[dim].values[0])}, {float(ds_field[dim].values[-1])})"
            )

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
    if 'X' in thr.dims:
        thr = thr.rename({'X': 'lon'})
    if 'Y' in thr.dims:
        thr = thr.rename({'Y': 'lat'})
    if 'X' in thr.coords:
        thr = thr.rename({'X': 'lon'})
    if 'Y' in thr.coords:
        thr = thr.rename({'Y': 'lat'})

    # Align threshold lon/lat to forecast grid (nearest)
    if 'lon' in thr.coords and 'lat' in thr.coords:
        thr = thr.sel(lon=field.lon, lat=field.lat, method='nearest')

    # --------- 3) Select month_day near init_date and align 'lead' length/coords ----------
    init_mdy = pd.Timestamp(init_date).strftime('%m-%d')
    if 'month_day' in thr.coords:
        chosen_mdy = _nearest_month_day_label(thr['month_day'].values, init_mdy)
        if chosen_mdy != init_mdy:
            print(f"[INFO] compute_exceedance using nearest month_day '{chosen_mdy}' for requested '{init_mdy}'")
        thr_day = thr.sel(month_day=chosen_mdy)
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

    # Dask-backed arrays can carry pathological 0-sized chunks that break
    # sliding-window overlap checks. Materialize both arrays so rolling is
    # deterministic across file/chunk layouts.
    if getattr(field, "chunks", None) is not None:
        field = field.load()
    if getattr(thr_var, "chunks", None) is not None:
        thr_var = thr_var.load()

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
def plot_exceedance_summary(probs, title, out_png, cmap='Reds', projection='platecarree'):
    """
    Plot mean across time_window for exceedance probabilities.
    probs: xr.DataArray with dims including 'time_window','lat','lon'
    """
    import matplotlib.pyplot as plt
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature

    # Normalize to a plottable 2D DataArray on (lat, lon).
    data = probs
    if isinstance(data, xr.Dataset):
        if not data.data_vars:
            print(f"[WARN] No variables to plot for exceedance summary: {title}")
            return False
        data = next(iter(data.data_vars.values()))

    if 'time_window' in data.dims:
        data = data.mean('time_window')

    if 'lat' not in data.dims or 'lon' not in data.dims:
        print(f"[WARN] Exceedance summary is not map-like; skipping plot: dims={data.dims}")
        return False

    # Squeeze any remaining singleton dims other than lat/lon.
    squeeze_dims = [d for d in data.dims if d not in ('lat', 'lon') and data.sizes.get(d, 1) == 1]
    if squeeze_dims:
        data = data.squeeze(squeeze_dims)

    # Do not average extra dimensions implicitly. If the data are not a 2D map,
    # report and skip so callers can diagnose the source dimensions explicitly.
    extra_dims = [d for d in data.dims if d not in ('lat', 'lon')]
    if extra_dims:
        print(f"[WARN] Exceedance summary has extra dims {extra_dims}; skipping plot.")
        return False

    if data.sizes.get('lat', 0) == 0 or data.sizes.get('lon', 0) == 0:
        print(f"[WARN] Empty lat/lon selection; skipping exceedance plot: {title}")
        return False

    finite = np.isfinite(data.values)
    if not finite.any():
        print(f"[WARN] Exceedance map has no finite values; skipping plot: {title}")
        return False

    map_projection = ccrs.Robinson() if projection == 'robinson' else ccrs.PlateCarree()
    fig, ax = plt.subplots(figsize=(8, 4), subplot_kw={'projection': map_projection})
    lon_vals = np.asarray(data['lon'].values, dtype=float)
    lat_vals = np.asarray(data['lat'].values, dtype=float)
    if projection == 'robinson':
        ax.set_global()
    elif lon_vals.size and lat_vals.size:
        ax.set_extent([
            float(np.nanmin(lon_vals)),
            float(np.nanmax(lon_vals)),
            float(np.nanmin(lat_vals)),
            float(np.nanmax(lat_vals)),
        ], crs=ccrs.PlateCarree())

    data.plot(ax=ax, transform=ccrs.PlateCarree(), cmap=cmap,
              vmin=5, vmax=90, extend='both', cbar_kwargs={'label':'%','shrink':0.8})
    ax.coastlines('110m', linewidth=1)
    ax.add_feature(cfeature.BORDERS, linewidth=0.6)
    ax.add_feature(cfeature.STATES,  linewidth=0.4)
    ax.set_title(title, fontsize=10)
    plt.tight_layout()
    plt.savefig(out_png, dpi=150, bbox_inches='tight')
    plt.close(fig)
    return True


def plot_exceedance_panels(probs, init_date, window, out_png_prefix, cmap='Reds'):
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
        lon_vals = np.asarray(data['lon'].values, dtype=float)
        lat_vals = np.asarray(data['lat'].values, dtype=float)
        if lon_vals.size and lat_vals.size:
            ax.set_extent([
                float(np.nanmin(lon_vals)),
                float(np.nanmax(lon_vals)),
                float(np.nanmin(lat_vals)),
                float(np.nanmax(lat_vals)),
            ], crs=ccrs.PlateCarree())
        data.plot(ax=ax, transform=ccrs.PlateCarree(), cmap=cmap,
              vmin=5, vmax=90, extend='both', cbar_kwargs={'label':'%','shrink':0.8})
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
    'nearest_mmdd_threshold',
    'compute_exceedance',
    'plot_exceedance_summary', 'plot_exceedance_panels',
]
