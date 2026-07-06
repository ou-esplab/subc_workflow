#!/usr/bin/env python3
"""
exceedance_utils.py
-------------------
Utilities for computing and plotting exceedance probabilities in the SubX
products pipeline.  These functions are independent of the PyCPT pipeline.

Public API
----------
- nearest_mmdd_threshold
- compute_exceedance
- plot_exceedance_summary
- plot_exceedance_panels
"""
from __future__ import annotations

import glob
import os
import re
from datetime import datetime
from typing import Optional, Tuple

import numpy as np
import pandas as pd
import xarray as xr


def _nearest_month_day_label(labels, init_mdy: str) -> str:
    """Return exact MM-DD label if present, otherwise nearest by circular day-of-year distance."""
    label_strs = [str(x) for x in labels]
    if init_mdy in label_strs:
        return init_mdy

    target = datetime.strptime("2000-" + init_mdy, "%Y-%m-%d")

    def score(lbl: str) -> int:
        try:
            d = datetime.strptime("2000-" + lbl, "%Y-%m-%d")
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

    target = datetime.strptime("2000" + mmdd, "%Y%m%d")

    def score(mmdd_token: str) -> int:
        d = datetime.strptime("2000" + mmdd_token, "%Y%m%d")
        diff = abs((d - target).days)
        return min(diff, 366 - diff)

    best_path, best_token = min(parsed, key=lambda rec: score(rec[1]))
    best_offset = score(best_token)
    if best_offset > max_offset_days:
        return None
    return best_path


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
    direction: str = 'above',
    aggregate: str = 'any',
) -> xr.DataArray:
    """
    Probability (or counts) that within each rolling window along 'lead', the forecast
    crosses the percentile threshold in the given direction/aggregation:

      - direction='above' (default): forecast > threshold (e.g. extreme heat/heavy rain).
      - direction='below': forecast < threshold (e.g. drought/dry-spell, cold snap).
      - aggregate='any' (default): flagged if AT LEAST ONE day in the window crosses
        (appropriate for a single extreme-event signal).
      - aggregate='all': flagged only if EVERY day in the window crosses (appropriate for
        a sustained condition, e.g. a multi-day dry spell -- with 'any', a low-percentile
        threshold near zero would trigger on almost any single dry day and not be a
        meaningful drought signal).

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
    if direction not in ('above', 'below'):
        raise ValueError(f"compute_exceedance: direction must be 'above' or 'below', got {direction!r}")
    if aggregate not in ('any', 'all'):
        raise ValueError(f"compute_exceedance: aggregate must be 'any' or 'all', got {aggregate!r}")
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

    # Align threshold lon/lat to forecast grid (nearest).
    # Some models (e.g. CFSv2) write percentile files with lat/lon as unindexed
    # dimensions (no coordinate values). Assign standard 1-degree global coords
    # inferred from the forecast field's lat ordering so sel() can proceed.
    if 'lat' in thr.dims and 'lat' not in thr.coords:
        n_lat = thr.sizes['lat']
        descending = 'lat' in field.coords and float(field['lat'].values[0]) > float(field['lat'].values[-1])
        lat_vals = np.linspace(90.0, -90.0, n_lat) if descending else np.linspace(-90.0, 90.0, n_lat)
        thr = thr.assign_coords(lat=('lat', lat_vals))
    if 'lon' in thr.dims and 'lon' not in thr.coords:
        n_lon = thr.sizes['lon']
        thr = thr.assign_coords(lon=('lon', np.linspace(0.0, 360.0 - 360.0 / n_lon, n_lon)))
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
        # We'll expose this as 0 windows by slicing below; keep shape consistent
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

    # Compare within window: does the window cross the threshold in the
    # requested direction, aggregated per the requested mode (any/all day)?
    crosses = (rolled_fcst > rolled_thresh) if direction == 'above' else (rolled_fcst < rolled_thresh)
    exceed_bool = crosses.any(dim="window_dim") if aggregate == 'any' else crosses.all(dim="window_dim")

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
              vmin=5, vmax=90, extend='both', cbar_kwargs={'label': '%', 'shrink': 0.8})
    ax.coastlines('110m', linewidth=1)
    ax.add_feature(cfeature.BORDERS, linewidth=0.6)
    ax.add_feature(cfeature.STATES, linewidth=0.4)
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
                  vmin=5, vmax=90, extend='both', cbar_kwargs={'label': '%', 'shrink': 0.8})
        ax.coastlines('110m', linewidth=1)
        ax.add_feature(cfeature.BORDERS, linewidth=0.6)
        ax.add_feature(cfeature.STATES, linewidth=0.4)
        ax.set_title(f"Exceedance Prob – step {i+1} – init {str(init_date)[:10]} – {window}d", fontsize=10)
        plt.tight_layout()
        out_png = f"{out_png_prefix}_step{str(i+1).zfill(2)}.png"
        plt.savefig(out_png, dpi=150, bbox_inches='tight')
        plt.close(fig)


__all__ = [
    'nearest_mmdd_threshold',
    'compute_exceedance',
    'plot_exceedance_summary',
    'plot_exceedance_panels',
]
