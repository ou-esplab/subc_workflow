#!/usr/bin/env python3
# coding: utf-8
"""
forecast.py – SubC weekly anomalies/MME + generic exceedance

What this does
--------------
1) Loads realtime SubC forecast files for each configured model/variable.
2) For each model, picks the most recent IC within the "forecast week" (Fri–Thu).
3) Converts ensemble members to an ensemble-mean field and computes anomalies
   against hindcast daily climatology on matching leads.
4) Aggregates to CPC weeks 1–4 (Sat–Fri):
     - precipitation (pr*) is summed after converting to mm/day
     - everything else is averaged
5) Builds SUBC‑MME as the model-mean of weekly anomalies.
6) Computes a single pooled multi-model-ensemble "exceedance probability" map
    (every model's ensemble members pooled together) using precomputed
    percentile fields (per-model, per-MMDD files).
7) Saves:
   - NetCDF of all models + MME weekly anomalies
   - Single MME exceedance NetCDF + PNG per configured region/week
   - manifest.json

Inputs (from config.yaml)
-------------------------
paths:
  rt_root, hc_root, out_weekly
models:  list of {group,name,vars,levels}
exceedance: {var, percentile, window_days, lev, max_fallback_days}
regions:    list of { name, subx:{lon,lat}, pycpt:{lon,lat} }
lon_convention: "0_360" or "negpos"

Usage
-----
python3 forecast.py --config config.yaml --fcstdate YYYYMMDD --save
"""

from __future__ import annotations
import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
import argparse
import glob
import os
from datetime import datetime
import numpy as np
import pandas as pd
import xarray as xr
import yaml

from utils.subc_pycpt_utils import (
    latest_thursday, fcst_week_dates, ensure_lon, safe_concat,
    weekly_reduce, save_manifest,
)
from utils.exceedance_utils import compute_exceedance, nearest_mmdd_threshold


DEBUG = False


def _env_flag(name: str) -> bool:
    val = os.environ.get(name, "")
    return str(val).strip().lower() in {"1", "true", "yes", "on"}


def _debug_print(msg: str) -> None:
    if DEBUG:
        print(msg)


def _select_week_window_from_probs(
    data: xr.DataArray,
    week_end: pd.Timestamp,
    week_num: int,
    region_name: str,
    allow_nearest_days: int = 3,
) -> xr.DataArray:
    if "time_window" not in data.dims:
        return data
    nwin = int(data.sizes.get("time_window", 0))
    if nwin == 0:
        return data

    target_end = np.datetime64(pd.Timestamp(week_end))
    tw = np.asarray(data["time_window"].values)
    if target_end in tw:
        return data.sel(time_window=target_end)

    if np.issubdtype(data["time_window"].dtype, np.datetime64):
        nearest_idx = int(np.argmin(np.abs(tw - target_end)))
        chosen = pd.Timestamp(tw[nearest_idx])
        delta_days = abs((chosen - pd.Timestamp(week_end)).days)
        if delta_days <= allow_nearest_days:
            _debug_print(
                f"[INFO] Using nearest time_window for week {week_num} in {region_name}: "
                f"target_end={pd.Timestamp(week_end).date()} chosen={chosen.date()}"
            )
            return data.isel(time_window=nearest_idx)

    print(
        f"[WARN] Missing time_window for week {week_num} in {region_name}; "
        f"target_end={pd.Timestamp(week_end).date()}"
    )
    return data.isel(time_window=0) * np.nan


def _remove_stale_exceedance_model_images(out_images: str, fcstdate: str) -> None:
    pattern = os.path.join(out_images, f"exceed_*_{fcstdate}.png")
    for path in glob.glob(pattern):
        fname = os.path.basename(path)
        # Keep only the new week-specific panel files (.._wk1.png .. _wk4.png).
        if fname.startswith("exceed_panel_") and "_wk" in fname:
            continue
        try:
            os.remove(path)
            _debug_print(f"[INFO] Removed stale exceedance model image: {path}")
        except OSError as exc:
            print(f"[WARN] Failed to remove stale exceedance model image {path}: {exc}")


def _remove_stale_legacy_products(out_images: str) -> None:
    patterns = [
        "2mTemp*.png",
        "Precip*.png",
        "500hPaGeopotentialHeight*.png",
    ]
    removed = 0
    for pattern in patterns:
        for path in glob.glob(os.path.join(out_images, pattern)):
            try:
                os.remove(path)
                removed += 1
            except OSError as exc:
                print(f"[WARN] Failed to remove stale legacy image {path}: {exc}")
    if removed:
        _debug_print(f"[INFO] Removed {removed} stale legacy product image(s).")

def _write_backward_compat_pr_anom_files(ds_save: xr.Dataset, out_data: str, fcstdate: str) -> None:
    """
    Write legacy-compatible precipitation anomaly files:
      - fcst_<date>.anom.pr_sfc.emin.nc
      - fcst_<date>.anom.pr_sfc.emean.nc
      - fcst_<date>.anom.pr_sfc.emax.nc

    Values are computed across ensemble-member dimension `M` from `pr_members`.
    """
    if "pr_members" not in ds_save:
        print("[WARN] Backward-compat postprocess skipped: variable 'pr_members' not found.")
        return

    src = ds_save["pr_members"]
    if "M" not in src.dims:
        print("[WARN] Backward-compat postprocess skipped: 'pr_members' has no 'M' dimension.")
        return

    stats = {
        "emin": src.min("M", skipna=True),
        "emean": src.mean("M", skipna=True),
        "emax": src.max("M", skipna=True),
    }

    def _legacy_model_var_name(model_id: str) -> str:
        if model_id == "SUBC-MME":
            return "MME"
        if "-" in model_id:
            return model_id.split("-", 1)[1]
        return model_id

    for stat_name, da in stats.items():
        # Build legacy layout: one variable per model on (time, lat, lon)
        # rather than a single variable with model dimension.
        da = da.rename({"week": "time"}) if "week" in da.dims else da
        if "time" in da.coords and np.issubdtype(da["time"].dtype, np.integer):
            start = pd.Timestamp(fcstdate) + pd.Timedelta(days=2)
            week_ends = [start + pd.Timedelta(days=6 + 7 * i) for i in range(int(da.sizes.get("time", 0)))]
            da = da.assign_coords(time=np.asarray(week_ends, dtype="datetime64[ns]"))

        out_ds = xr.Dataset()
        if "lon" in da.coords:
            out_ds = out_ds.assign_coords(lon=da["lon"]) 
            out_ds["lon"].attrs["units"] = "degrees_east"
        if "lat" in da.coords:
            out_ds = out_ds.assign_coords(lat=da["lat"])
            out_ds["lat"].attrs["units"] = "degrees_north"
        if "time" in da.coords:
            out_ds = out_ds.assign_coords(time=da["time"])
            out_ds["time"].attrs["standard_name"] = "time"
            out_ds["time"].attrs["long_name"] = "Time of measurements"

        if "model" in da.dims:
            for model_id in da["model"].values:
                model_name = str(model_id)
                legacy_var = _legacy_model_var_name(model_name)
                model_slice = da.sel(model=model_id).astype("float64")
                model_slice = model_slice.transpose("time", "lat", "lon")
                out_ds[legacy_var] = model_slice
                out_ds[legacy_var].attrs["long_name"] = f"{model_name} {fcstdate}"
                out_ds[legacy_var].attrs["units"] = "mmday-1"
        else:
            out_ds["MME"] = da.astype("float64").transpose("time", "lat", "lon")
            out_ds["MME"].attrs["long_name"] = f"SUBC-MME {fcstdate}"
            out_ds["MME"].attrs["units"] = "mmday-1"

        out_ds.attrs["units"] = "mmday-1"
        out_ds.attrs["source_file"] = f"subx_mme_anoms_wk_1-4_{fcstdate}.nc"
        out_ds.attrs["description"] = (
            "Backward-compatible SubC precipitation anomaly product "
            f"({stat_name}) derived from pr_members across ensemble dimension M"
        )

        out_file = os.path.join(out_data, f"fcst_{fcstdate}.anom.pr_sfc.{stat_name}.nc")
        out_ds.to_netcdf(out_file)
        print(f"[SAVE] {out_file}")


def _prepare_domain_view(da: xr.DataArray, lon_bounds: tuple[float, float], lat_bounds: tuple[float, float]) -> xr.DataArray:
    da_plot = da.assign_coords(lon=xr.where(da["lon"] > 180, da["lon"] - 360, da["lon"])).sortby("lon")

    def _slice_for_coord(coord: xr.DataArray, low: float, high: float) -> slice:
        if coord.size == 0:
            return slice(low, high)
        first = float(coord.values[0])
        last = float(coord.values[-1])
        if first <= last:
            return slice(low, high)
        return slice(high, low)

    lon_sel = _slice_for_coord(da_plot["lon"], lon_bounds[0], lon_bounds[1])
    lat_sel = _slice_for_coord(da_plot["lat"], lat_bounds[0], lat_bounds[1])
    return da_plot.sel(lon=lon_sel, lat=lat_sel)


def _write_region_weekly_data(
    ds_subx: xr.Dataset,
    var_name: str,
    out_data: str,
    fcstdate: str,
    units: str,
    filename_prefix: str,
    domain_name: str,
    lon_bounds: tuple[float, float],
    lat_bounds: tuple[float, float],
) -> None:
    if var_name not in ds_subx:
        print(f"[WARN] Skipping {filename_prefix} regional data ({var_name} not present).")
        return

    for week in [1, 2, 3, 4]:
        da = _prepare_domain_view(ds_subx[var_name].sel(week=week), lon_bounds, lat_bounds)
        out_ds = da.to_dataset(name=var_name)
        out_ds.attrs["fcstdate"] = fcstdate
        out_ds.attrs["domain_name"] = domain_name
        out_ds.attrs["units"] = units
        out_ds.attrs["week"] = week

        out_file = os.path.join(out_data, f"{filename_prefix}{domain_name}Week{week}.nc")
        out_ds.to_netcdf(out_file)
        print(f"[SAVE] {out_file}")

    week34_aggregate = "sum" if var_name.startswith("pr") else "mean"
    if week34_aggregate == "sum":
        da_34 = _prepare_domain_view(ds_subx[var_name].sel(week=[3, 4]).sum("week"), lon_bounds, lat_bounds)
        week34_units = units.replace("mm/week", "mm/2-weeks")
    else:
        da_34 = _prepare_domain_view(ds_subx[var_name].sel(week=[3, 4]).mean("week"), lon_bounds, lat_bounds)
        week34_units = units

    out_ds = da_34.to_dataset(name=var_name)
    out_ds.attrs["fcstdate"] = fcstdate
    out_ds.attrs["domain_name"] = domain_name
    out_ds.attrs["units"] = week34_units
    out_ds.attrs["week"] = "3-4"
    out_ds.attrs["aggregation"] = week34_aggregate

    out_file = os.path.join(out_data, f"{filename_prefix}{domain_name}Weeks34.nc")
    out_ds.to_netcdf(out_file)
    print(f"[SAVE] {out_file}")


def _unique(items: list[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        if item and item not in out:
            out.append(item)
    return out


def _candidate_model_names(group: str, server_model: str, model_map: dict) -> list[str]:
    mapped = model_map.get(f"{group}-{server_model}", model_map.get(server_model, server_model))
    return _unique([mapped, server_model])


def _extract_init_date(path: str) -> pd.Timestamp | None:
    base = os.path.basename(path)
    if not base.endswith(".daily.nc"):
        return None
    token = base[: -len(".daily.nc")].rsplit("_", 1)
    if len(token) != 2 or len(token[1]) != 8 or not token[1].isdigit():
        return None
    try:
        return pd.Timestamp(token[1])
    except Exception:
        return None


def _pick_realtime_file(
    rt_root: str,
    group: str,
    model_candidates: list[str],
    var: str,
    fcst_ts: pd.Timestamp,
    week_start: pd.Timestamp,
    week_end: pd.Timestamp,
) -> tuple[str, str, pd.Timestamp] | None:
    in_week: list[tuple[pd.Timestamp, str, str]] = []

    for candidate in model_candidates:
        patt = f"{rt_root}/{group}-{candidate}/forecast/{var}/{var}_{group}-{candidate}_*.daily.nc"
        for path in sorted(glob.glob(patt)):
            init_ts = _extract_init_date(path)
            if init_ts is None or init_ts > fcst_ts:
                continue
            if week_start <= init_ts <= week_end:
                rec = (init_ts, path, candidate)
                in_week.append(rec)

    if in_week:
        init_ts, path, candidate = max(in_week, key=lambda rec: rec[0])
        return path, candidate, init_ts
    return None


def _resolve_climo_file(
    hc_root: str,
    group: str,
    var: str,
    plev,
    mmdd_last: str,
    model_candidates: list[str],
) -> str | None:
    for candidate in model_candidates:
        climo_fname = (
            f"{hc_root}/{var}{plev}/daily/climo/{group}-{candidate}/"
            f"{var}_{group}-{candidate}_{mmdd_last}.climo.p.nc"
        )
        if os.path.exists(climo_fname):
            return climo_fname
    return None


def _resolve_panel_models(
    available_models: list[str],
    panel_models: list[str] | None,
) -> list[str]:
    available = _unique([str(m) for m in available_models])
    available_set = set(available)

    def _resolve_configured_model_id(configured_model: str) -> str:
        if configured_model in available_set:
            return configured_model

        if configured_model.endswith("_5daily"):
            stripped = configured_model[: -len("_5daily")]
            if stripped in available_set:
                return stripped

        if "-" in configured_model:
            group, _ = configured_model.split("-", 1)
            group_prefix = f"{group}-"
            for candidate in available:
                if not candidate.startswith(group_prefix):
                    continue
                if candidate.startswith(configured_model) or configured_model.startswith(candidate):
                    return candidate

        return configured_model

    preferred_order = [
        "ESRL-FIMr1p1",
        "RSMAS-CCSM4",
        "EMC-GEFSv12_CPC",
        "GMAO-GEOS_V2p1",
        "NCEP-CFSv2",
        "ECCC-GEPS8",
        "SUBC-MME",
    ]

    if panel_models:
        configured = _unique([str(m) for m in panel_models])
        configured_set = set(configured)
        configured_resolved = _unique([
            _resolve_configured_model_id(m)
            for m in configured
            if m != "SUBC-MME"
        ])
        configured_resolved_set = set(configured_resolved)

        models = [m for m in preferred_order if m in configured_resolved_set or m in available_set]
        extras_configured = [m for m in configured_resolved if m not in models]
        extras_available = [m for m in available if m not in models and m != "SUBC-MME"]
        models.extend(extras_configured)
        models.extend(extras_available)

        if ("SUBC-MME" in configured_set or "SUBC-MME" in available_set) and "SUBC-MME" not in models:
            models.append("SUBC-MME")
        return models

    real_models = [m for m in available if m != "SUBC-MME"]
    return real_models + (["SUBC-MME"] if "SUBC-MME" in available_set else [])


def _plot_weekly_panels(
    ds_subx: xr.Dataset,
    var_name: str,
    out_images: str,
    fcstdate: str,
    title_prefix: str,
    units: str,
    filename_prefix: str,
    domain_name: str,
    lon_bounds: tuple[float, float],
    lat_bounds: tuple[float, float],
    levels: np.ndarray,
    panel_models: list[str] | None = None,
) -> None:
    if var_name not in ds_subx:
        print(f"[WARN] Skipping {filename_prefix} plots ({var_name} not present).")
        return

    import math
    import matplotlib.pyplot as plt
    import matplotlib.colors as mcolors
    import matplotlib.path as mpath
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature

    available_models = [str(m) for m in ds_subx["model"].values]
    available_set = set(available_models)

    models = _resolve_panel_models(available_models, panel_models)

    ncols = 3
    nrows = math.ceil(len(models) / ncols)
    # Choose colormap based on variable name.
    # Precipitation uses brown (negative) to green (positive),
    # while temperature/heights use blue (negative) to red (positive).
    if "pr" in var_name:
        cmap = plt.get_cmap("BrBG")
    elif "hgt" in var_name or "psl" in var_name:
        cmap = plt.get_cmap("RdBu_r")
    else:
        cmap = plt.get_cmap("RdBu_r")
    
    # Center colorbar around zero for anomalies using TwoSlopeNorm
    vmin, vmax = levels.min(), levels.max()
    norm = mcolors.TwoSlopeNorm(vmin=vmin, vcenter=0, vmax=vmax)

    is_polar_geopotential = ("hgt" in var_name) or ("zg" in var_name)
    is_north_america = domain_name == "NorthAmerica"
    is_global = domain_name in ("Global", "NorthernHemisphere")
    is_regional = not is_polar_geopotential and not is_global
    if is_polar_geopotential:
        projection = ccrs.NorthPolarStereo()
    elif is_regional:
        projection = ccrs.PlateCarree()
    else:
        projection = ccrs.Robinson()

    for week in [1, 2, 3, 4]:
        fig, axes = plt.subplots(
            nrows,
            ncols,
            figsize=(4.7 * ncols, 3.15 * nrows + 1.7),
            subplot_kw={"projection": projection},
            squeeze=False,
        )
        valid_week_end = pd.Timestamp(fcstdate) + pd.Timedelta(days=(week * 7 + 1))
        fig.suptitle(
            f"SubC Week {week} {title_prefix}: Valid week ending {valid_week_end:%b %d, %Y}",
            fontsize=14,
            y=0.985,
        )

        mappable = None
        for idx, model_id in enumerate(models):
            row, col = divmod(idx, ncols)
            ax = axes[row][col]
            if is_polar_geopotential:
                ax.set_extent([-180, 180, 20, 90], crs=ccrs.PlateCarree())
                theta = np.linspace(0.0, 2.0 * np.pi, 100)
                center = np.array([0.5, 0.5])
                radius = 0.5
                circle = mpath.Path(np.column_stack([np.sin(theta), np.cos(theta)]) * radius + center)
                ax.set_boundary(circle, transform=ax.transAxes)
            elif is_regional:
                ax.set_extent([lon_bounds[0], lon_bounds[1], lat_bounds[0], lat_bounds[1]], crs=ccrs.PlateCarree())
            else:
                ax.set_global()

            if model_id in available_set:
                ax.coastlines("110m", linewidth=0.8)
                ax.add_feature(cfeature.BORDERS, linewidth=0.4)
                if is_regional:
                    ax.add_feature(cfeature.STATES, linewidth=0.2)
                da = ds_subx[var_name].sel(model=model_id, week=week)
                da = _prepare_domain_view(da, lon_bounds, lat_bounds)

                finite = np.isfinite(da.values)
                if finite.any():
                    mappable = ax.contourf(
                        da["lon"],
                        da["lat"],
                        da,
                        levels=levels,
                        cmap=cmap,
                        norm=norm,
                        transform=ccrs.PlateCarree(),
                        extend="both",
                    )
                else:
                    ax.text(0.5, 0.5, "No data", transform=ax.transAxes, ha="center", va="center", fontsize=10)
            else:
                ax.text(0.5, 0.5, "Missing model", transform=ax.transAxes, ha="center", va="center", fontsize=10)
            try:
                ax.spines["geo"].set_linewidth(0.8)
            except Exception:
                pass
            # Extract IC date and ensemble count for title.
            ic_date = ds_subx.coords.get("init_date")
            if model_id in available_set and ic_date is not None and "model" in ic_date.dims:
                ic_raw = ic_date.sel(model=model_id).item()
                ic_date_str = str(ic_raw)[:10]
            else:
                ic_date_str = "Unknown"
            if model_id in available_set and "nens" in ds_subx.coords and "model" in ds_subx["nens"].dims:
                nens_raw = ds_subx["nens"].sel(model=model_id).item()
                try:
                    nens = int(nens_raw)
                except (TypeError, ValueError):
                    nens = 0
            else:
                nens = 0

            if model_id in available_set:
                ax.set_title(f"{model_id} (IC: {ic_date_str}; {nens} Ens)", fontsize=10, pad=4)
            else:
                ax.set_title(f"{model_id} (IC: N/A; N/A Ens)", fontsize=10, pad=4)

        for idx in range(len(models), nrows * ncols):
            row, col = divmod(idx, ncols)
            axes[row][col].axis("off")

        fig.subplots_adjust(top=0.90, bottom=0.16, left=0.04, right=0.98, wspace=0.07, hspace=0.30)
        if mappable is not None:
            cax = fig.add_axes([0.18, 0.06, 0.64, 0.03])
            cbar = fig.colorbar(
                mappable,
                cax=cax,
                orientation="horizontal",
                ticks=levels,
            )
            cbar.ax.tick_params(labelsize=8)
            cbar.set_label(units, fontsize=10)

        out_png = os.path.join(out_images, f"{filename_prefix}{domain_name}Week{week}.png")
        fig.savefig(out_png, dpi=150)
        plt.close(fig)
        print(f"[SAVE] {out_png}")

    week34_is_total = var_name.startswith("pr")
    if week34_is_total:
        da_34 = ds_subx[var_name].sel(week=[3, 4]).sum("week")
        week34_title_prefix = title_prefix.replace("mm/week", "mm/2-weeks")
        week34_units = units.replace("mm/week", "mm/2-weeks")
    else:
        da_34 = ds_subx[var_name].sel(week=[3, 4]).mean("week")
        week34_title_prefix = title_prefix
        week34_units = units

    fig, axes = plt.subplots(
        nrows,
        ncols,
        figsize=(4.7 * ncols, 3.15 * nrows + 1.7),
        subplot_kw={"projection": projection},
        squeeze=False,
    )
    valid_34_end = pd.Timestamp(fcstdate) + pd.Timedelta(days=29)
    fig.suptitle(
        f"SubC Week 3-4 {week34_title_prefix}: {'2-week total' if week34_is_total else '2-week mean'} ending {valid_34_end:%b %d, %Y}",
        fontsize=14,
        y=0.985,
    )

    mappable = None
    for idx, model_id in enumerate(models):
        row, col = divmod(idx, ncols)
        ax = axes[row][col]
        if is_polar_geopotential:
            ax.set_extent([-180, 180, 20, 90], crs=ccrs.PlateCarree())
            theta = np.linspace(0.0, 2.0 * np.pi, 100)
            center = np.array([0.5, 0.5])
            radius = 0.5
            circle = mpath.Path(np.column_stack([np.sin(theta), np.cos(theta)]) * radius + center)
            ax.set_boundary(circle, transform=ax.transAxes)
        elif is_regional:
            ax.set_extent([lon_bounds[0], lon_bounds[1], lat_bounds[0], lat_bounds[1]], crs=ccrs.PlateCarree())
        else:
            ax.set_global()

        if model_id in available_set:
            ax.coastlines("110m", linewidth=0.8)
            ax.add_feature(cfeature.BORDERS, linewidth=0.4)
            if is_regional:
                ax.add_feature(cfeature.STATES, linewidth=0.2)
            da = _prepare_domain_view(da_34.sel(model=model_id), lon_bounds, lat_bounds)
            finite = np.isfinite(da.values)
            if finite.any():
                mappable = ax.contourf(
                    da["lon"],
                    da["lat"],
                    da,
                    levels=levels,
                    cmap=cmap,
                    norm=norm,
                    transform=ccrs.PlateCarree(),
                    extend="both",
                )
            else:
                ax.text(0.5, 0.5, "No data", transform=ax.transAxes, ha="center", va="center", fontsize=10)
        else:
            ax.text(0.5, 0.5, "Missing model", transform=ax.transAxes, ha="center", va="center", fontsize=10)
        try:
            ax.spines["geo"].set_linewidth(0.8)
        except Exception:
            pass
        # Extract IC date and ensemble count for title.
        ic_date = ds_subx.coords.get("init_date")
        if model_id in available_set and ic_date is not None and "model" in ic_date.dims:
            ic_raw = ic_date.sel(model=model_id).item()
            ic_date_str = str(ic_raw)[:10]
        else:
            ic_date_str = "Unknown"
        if model_id in available_set and "nens" in ds_subx.coords and "model" in ds_subx["nens"].dims:
            nens_raw = ds_subx["nens"].sel(model=model_id).item()
            try:
                nens = int(nens_raw)
            except (TypeError, ValueError):
                nens = 0
        else:
            nens = 0

        if model_id in available_set:
            ax.set_title(f"{model_id} (IC: {ic_date_str}; {nens} Ens)", fontsize=10, pad=4)
        else:
            ax.set_title(f"{model_id} (IC: N/A; N/A Ens)", fontsize=10, pad=4)
    for idx in range(len(models), nrows * ncols):
        row, col = divmod(idx, ncols)
        axes[row][col].axis("off")

    fig.subplots_adjust(top=0.90, bottom=0.16, left=0.04, right=0.98, wspace=0.07, hspace=0.30)
    if mappable is not None:
        cax = fig.add_axes([0.18, 0.06, 0.64, 0.03])
        cbar = fig.colorbar(
            mappable,
            cax=cax,
            orientation="horizontal",
            ticks=levels,
        )
        cbar.ax.tick_params(labelsize=8)
        cbar.set_label(week34_units, fontsize=10)

    out_png = os.path.join(out_images, f"{filename_prefix}{domain_name}Weeks34.png")
    fig.savefig(out_png, dpi=150)
    plt.close(fig)
    print(f"[SAVE] {out_png}")


def _plot_exceedance_mme_map(
    out_images: str,
    fcstdate: str,
    region_name: str,
    var: str,
    probs: xr.DataArray,
    pct: int = 95,
    direction: str = "above",
    aggregate: str = "any",
    product_tag: str | None = None,
    week_num: int = 1,
    week_start: pd.Timestamp | None = None,
    week_end: pd.Timestamp | None = None,
    global_projection: bool = False,
    mme_member_count: int = 0,
) -> None:
    import matplotlib.pyplot as plt
    import matplotlib.colors as mcolors
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature
    import cartopy.util as cutil

    if week_start is None or week_end is None:
        wk_start = pd.Timestamp(fcstdate) + pd.Timedelta(days=(2 + (week_num - 1) * 7))
        wk_end = wk_start + pd.Timedelta(days=6)
    else:
        wk_start = pd.Timestamp(week_start)
        wk_end = pd.Timestamp(week_end)

    valid_str = f"{wk_start.strftime('%b %d')} – {wk_end.strftime('%b %d, %Y')}"

    if product_tag is None:
        product_tag = f"p{pct}_{direction}"

    verb = "Exceeding" if direction == "above" else "Falling Below"
    day_phrase = "At Least One Day" if aggregate == "any" else "Every Day"
    projection = ccrs.Robinson() if global_projection else ccrs.PlateCarree()
    fig, ax = plt.subplots(figsize=(9.4, 5.7), subplot_kw={"projection": projection})
    fig.suptitle(
        f"Probability of {verb} {pct}th Percentile on {day_phrase} During Week {week_num} ({valid_str})\n"
        f"{var} - {region_name} | MME Blend, {int(mme_member_count)} Ens",
        fontsize=13,
        y=0.98,
    )

    levels = np.arange(5, 95, 5)
    cmap = plt.get_cmap("Reds", len(levels) - 1)
    norm = mcolors.BoundaryNorm(levels, cmap.N)

    data = _select_week_window_from_probs(probs, wk_end, week_num, region_name)

    if "lat" in data.dims and "lon" in data.dims and data.sizes.get("lat", 0) and data.sizes.get("lon", 0):
        lon_vals = np.asarray(data["lon"].values, dtype=float)
        lat_vals = np.asarray(data["lat"].values, dtype=float)
        if lon_vals.size and lat_vals.size:
            extent = [float(np.nanmin(lon_vals)), float(np.nanmax(lon_vals)), float(np.nanmin(lat_vals)), float(np.nanmax(lat_vals))]
        else:
            extent = None
    else:
        extent = None

    if global_projection:
        ax.set_global()
    elif extent is not None:
        ax.set_extent(extent, crs=ccrs.PlateCarree())
    ax.coastlines("110m", linewidth=0.8)
    ax.add_feature(cfeature.BORDERS, linewidth=0.4)
    if not global_projection:
        ax.add_feature(cfeature.STATES, linewidth=0.2)

    mappable = None
    if "lat" not in data.dims or "lon" not in data.dims:
        ax.text(0.5, 0.5, "Non-map output", transform=ax.transAxes, ha="center", va="center", fontsize=10)
    elif data.sizes.get("lat", 0) == 0 or data.sizes.get("lon", 0) == 0:
        ax.text(0.5, 0.5, "No data", transform=ax.transAxes, ha="center", va="center", fontsize=10)
    elif np.isfinite(data.values).any():
        vals = np.asarray(data.values, dtype=float)
        finite_vals = vals[np.isfinite(vals)]
        if finite_vals.size:
            q10, q50, q90 = np.nanpercentile(finite_vals, [10, 50, 90])
            _debug_print(
                f"[EXCEED-DIAG] region={region_name} model=SUBC-MME "
                f"min={np.nanmin(finite_vals):.1f} p10={q10:.1f} p50={q50:.1f} p90={q90:.1f} "
                f"max={np.nanmax(finite_vals):.1f} std={np.nanstd(finite_vals):.2f}"
            )
        plot_vals = np.asarray(data.values, dtype=float)
        plot_lon = np.asarray(data["lon"].values, dtype=float)
        if global_projection:
            # The lon grid runs 0..359 without wrapping back to 360/0, which
            # leaves a visible seam at the prime meridian in a global map;
            # add_cyclic_point closes the circle by duplicating the first
            # column at the end.
            plot_vals, plot_lon = cutil.add_cyclic_point(plot_vals, coord=plot_lon)
        mappable = ax.contourf(
            plot_lon,
            data["lat"],
            plot_vals,
            levels=levels,
            cmap=cmap,
            norm=norm,
            transform=ccrs.PlateCarree(),
            extend="both",
        )
    else:
        ax.text(0.5, 0.5, "No data", transform=ax.transAxes, ha="center", va="center", fontsize=10)

    try:
        ax.spines["geo"].set_linewidth(0.8)
    except Exception:
        pass

    fig.subplots_adjust(top=0.85, bottom=0.16, left=0.04, right=0.98)
    if mappable is not None:
        cax = fig.add_axes([0.18, 0.06, 0.64, 0.03])
        cbar = fig.colorbar(mappable, cax=cax, orientation="horizontal")
        cbar.set_label("%", fontsize=10)

    region_dir = os.path.join(out_images, region_name)
    os.makedirs(region_dir, exist_ok=True)
    out_png = os.path.join(region_dir, f"exceed_mme_{var}_{product_tag}_{region_name}_{fcstdate}_wk{week_num}.png")
    fig.savefig(out_png, dpi=150)
    plt.close(fig)
    print(f"[SAVE] {out_png}")


def _plot_legacy_weekly_products(
    ds_subx: xr.Dataset,
    out_images: str,
    out_data: str,
    fcstdate: str,
    panel_models: list[str],
    cfg: dict,
) -> None:
    def _to_negpos(lon: float) -> float:
        return lon - 360 if lon > 180 else lon

    # Build domain list from config regions; convert 0-360 lons to -180/180
    domains = []
    for r in cfg.get("regions", []):
        name = r["name"]
        bounds = r.get("subx", {})
        raw_lons = bounds.get("lon", [-180, 180])
        lo_neg = _to_negpos(raw_lons[0])
        hi_neg = _to_negpos(raw_lons[1])
        # [0, 360] collapses to [0, 0] after negpos; treat as full global span
        if lo_neg == hi_neg:
            lo_neg, hi_neg = -180, 180
        lats = bounds.get("lat", [-90, 90])
        domains.append((name, (lo_neg, hi_neg), (lats[0], lats[1])))

    for domain_name, lon_bounds, lat_bounds in domains:
        _plot_weekly_panels(
            ds_subx,
            var_name="tas",
            out_images=out_images,
            fcstdate=fcstdate,
            title_prefix="2m Temperature Anomalies (C)",
            units="C",
            filename_prefix="2mTemp",
            domain_name=domain_name,
            lon_bounds=lon_bounds,
            lat_bounds=lat_bounds,
            levels=np.array([-4, -3, -2.5, -2, -1.5, -1, -0.2, 0.2, 0.5, 1, 1.5, 2, 2.5, 3, 4]),
            panel_models=panel_models,
        )
        _write_region_weekly_data(
            ds_subx,
            var_name="tas",
            out_data=out_data,
            fcstdate=fcstdate,
            units="C",
            filename_prefix="2mTemp",
            domain_name=domain_name,
            lon_bounds=lon_bounds,
            lat_bounds=lat_bounds,
        )

    for domain_name, lon_bounds, lat_bounds in domains:
        _plot_weekly_panels(
            ds_subx,
            var_name="pr",
            out_images=out_images,
            fcstdate=fcstdate,
            title_prefix="Precipitation Anomalies (mm/week)",
            units="mm/week",
            filename_prefix="Precip",
            domain_name=domain_name,
            lon_bounds=lon_bounds,
            lat_bounds=lat_bounds,
            levels=np.array([-35, -25, -15, -10, -5, -2, 2, 5, 10, 15, 25, 35]),
            panel_models=panel_models,
        )

    _plot_weekly_panels(
        ds_subx,
        var_name="zg_500",
        out_images=out_images,
        fcstdate=fcstdate,
        title_prefix="500 hPa Geopotential Height Anomalies (m)",
        units="m",
        filename_prefix="500hPaGeopotentialHeight",
        domain_name="NorthernHemisphere",
        lon_bounds=(-180, 180),
        lat_bounds=(10, 90),
        levels=np.array([-200, -150, -100, -60, -30, -10, 10, 30, 60, 100, 150, 200]),
        panel_models=panel_models,
    )

def main():
    global DEBUG

    ap = argparse.ArgumentParser()
    ap.add_argument('--config', required=True, help='Path to config.yaml')
    ap.add_argument('--fcstdate', required=False, help='YYYYMMDD (optional; defaults to latest Thursday)')
    ap.add_argument('--save', action='store_true', help='Write NetCDF + manifest to disk')
    ap.add_argument('--allow-empty-input', action='store_true', help='Exit successfully when no realtime model files are available')
    ap.add_argument('--debug', action='store_true', help='Enable verbose diagnostic logging')
    ap.add_argument('--skip-exceedance', action='store_true', help='Skip exceedance probability computation')
    args = ap.parse_args()

    DEBUG = bool(args.debug or _env_flag("SUBX_PRODUCTS_DEBUG"))
    if DEBUG:
        print("[DEBUG] products debug logging enabled")

    # ---- Config & date handling ----
    with open(args.config, 'r', encoding='utf-8') as config_file:
        cfg = yaml.safe_load(config_file) or {}
    model_map = cfg.get("model_name_map", {}) or {}

    fcstdate = args.fcstdate or latest_thursday(cfg.get('fcstdate'))
    print(f"[INFO] fcstdate={fcstdate}")

    rt_root    = cfg['paths']['rt_root']
    hc_root    = cfg['paths']['hc_root']
    out_weekly = cfg['paths']['out_weekly']
    lon_conv   = cfg.get('lon_convention', '0_360')
    week_dates = fcst_week_dates(fcstdate)
    fcst_ts = pd.Timestamp(fcstdate)
    init_window_start = fcst_ts - pd.Timedelta(days=7)
    init_window_end = fcst_ts - pd.Timedelta(days=1)

    out_images = os.path.join(out_weekly, fcstdate, 'images')
    out_data   = os.path.join(out_weekly, fcstdate, 'data')
    os.makedirs(out_images, exist_ok=True)
    os.makedirs(out_data,   exist_ok=True)
    _remove_stale_exceedance_model_images(out_images, fcstdate)

    ds_anoms_by_model = []
    ds_member_anoms_by_model = []
    ds_full_by_model  = []
    model_init_dates: dict[str, pd.Timestamp] = {}
    model_ensemble_counts: dict[str, int] = {}

    for m in cfg["models"]:
        group = m["group"]
        server_model = m["name"]
        model_candidates = _candidate_model_names(group, server_model, model_map)
        local_model = model_candidates[0]

        # product_vars/product_levels let a model ingest (archive) a wider variable
        # set than its hindcast source can support for climo/percentiles -- e.g.
        # GEOS_V3's hindcast portal only has pr/tas, so forecast products for it
        # must stay restricted to those even though vars/levels (used by the
        # ingest scripts) covers all 7 real-time variables for archiving.
        varlist, levlist = m.get("product_vars", m["vars"]), m.get("product_levels", m["levels"])

        # Canonical model_id for coords/labels/outputs
        model_id_local = f"{group}-{local_model}"
        _debug_print(f"[MODEL] server={group}-{server_model}  local={model_id_local}")

        var_dsets_anom = []
        var_dsets_member_anom = []
        var_dsets_full = []

        for var, plev in zip(varlist, levlist):
            chosen = _pick_realtime_file(
                rt_root=rt_root,
                group=group,
                model_candidates=model_candidates,
                var=var,
                fcst_ts=fcst_ts,
                week_start=init_window_start,
                week_end=init_window_end,
            )
            if not chosen:
                window_start = init_window_start.strftime("%Y%m%d")
                window_end = init_window_end.strftime("%Y%m%d")
                print(
                    f"  - excluded outside recency window [{window_start}..{window_end}] "
                    f"under candidates: {', '.join(f'{group}-{name}' for name in model_candidates)}"
                )
                continue

            fp, chosen_model_name, chosen_init = chosen
            try:
                ds = xr.open_dataset(fp)
            except OSError as exc:
                print(f"  [WARN] Skipping {group}-{chosen_model_name} {var}: cannot open {fp} ({exc})")
                continue
            _debug_print(f"  [INFO] Using init for {var}: {group}-{chosen_model_name} {chosen_init:%Y%m%d}")

            model_id_local = f"{group}-{chosen_model_name}"
            prev_init = model_init_dates.get(model_id_local)
            if prev_init is None or chosen_init > prev_init:
                model_init_dates[model_id_local] = chosen_init
            if "P" in ds[var].dims:
                ds = ds.sel(P=int(plev))
            ds["S"] = ds["S"].dt.floor("D")
            ds = ds.dropna("S", how="all")
            if ds.S.size == 0:
                print(f"  - all missing after drop: {var}")
                continue
            if ds.S.size > 1:
                # Multiple sub-daily inits on the same day (e.g. CFSv2 00z/06z/12z/18z).
                # Treat each init as a separate ensemble member by collapsing S into M.
                init_date = ds["S"].values[-1]
                parts = []
                m_start = 1
                for i in range(ds.S.size):
                    part = ds.isel(S=i, drop=True)
                    if "M" in part.dims:
                        n = part.sizes["M"]
                        part = part.assign_coords(
                            M=np.arange(m_start, m_start + n, dtype=np.float32)
                        )
                        m_start += n
                    else:
                        part = part.assign_coords(M=float(m_start)).expand_dims("M")
                        m_start += 1
                    parts.append(part)
                ds = xr.concat(parts, dim="M").assign_coords(S=init_date)
            else:
                ds = ds.isel(S=-1)

            # Update ensemble count after S-stacking so sub-daily inits
            # (e.g. CFSv2 4 inits × 4 members = 16) are counted correctly.
            model_ensemble_counts[model_id_local] = max(
                model_ensemble_counts.get(model_id_local, 0), ds.sizes.get("M", 1)
            )

            lead = ds["L"]
            if np.issubdtype(lead.dtype, np.timedelta64):
                lead_offsets = lead.dt.floor("D")
            else:
                units = str(lead.attrs.get("units", "")).strip().lower()
                if "hour" in units or units in {"h", "hr", "hrs"}:
                    unit = "h"
                elif "min" in units:
                    unit = "m"
                elif "sec" in units:
                    unit = "s"
                else:
                    # Most SubX L coordinates are day-based numeric leads.
                    unit = "D"
                lead_offsets = xr.DataArray(
                    pd.to_timedelta(lead.values, unit=unit).floor("D"),
                    dims=lead.dims,
                    coords=lead.coords,
                )
            ds["L"] = ds["S"].values + lead_offsets
            ds = ds.drop_vars("S", errors="ignore")
            ds = ds.rename({"X": "lon", "Y": "lat", "L": "lead"})
            ds["lon"].attrs["units"] = "degrees_east"
            ds["lat"].attrs["units"] = "degrees_north"
            # Some models write lat descending (90 -> -90, e.g. CPC-sourced CFS/GEFS)
            # while others are ascending; sort so every model shares one convention
            # before anything downstream merges models along a shared lat axis.
            ds = ensure_lon(ds, lon_conv).sortby('lat').assign_coords(model=model_id_local)   # <-- local id
            if "M" in ds.dims and "M" not in ds.coords:
                ds = ds.assign_coords(M=np.arange(1, int(ds.sizes["M"]) + 1, dtype=np.int32))

            # ---- Climatology from precomputed files ----
            mmdd_last   = pd.to_datetime(week_dates[-1]).strftime("%m%d")
            climo_fname = _resolve_climo_file(hc_root, group, var, plev, mmdd_last, model_candidates)
            if not climo_fname:
                print(f"  - missing climatology for {group}-{chosen_model_name} {var} mmdd={mmdd_last}")
                continue
            clim = xr.open_dataset(climo_fname).rename({"time": "lead"})

            # model-specific adjustments — unchanged
            if server_model == "GEFSv12":
                if var == "pr":
                    clim = clim / 86400.0
            elif server_model == "GEFSv12_CPC":
                leads = pd.date_range(ds["lead"][0].values - pd.Timedelta(days=1),
                                      ds["lead"][-1].values, freq="D")
                clim["lead"] = leads
                if var == "pr":
                    clim = clim / 86400.0
            else:
                clim["lead"] = ds["lead"]

            if var == "psl":
                ds["psl"]   = ds["psl"] / 100.0
                clim["psl"] = clim["psl"] / 100.0

            vout = var if "P" not in ds[var].coords else f"{var}_{plev}"
            if vout != var:
                ds = ds.rename({var: vout})

            member_anom = ds[vout] - clim[var]
            if "dayofyear" in member_anom.dims:
                member_anom = member_anom.drop_dims("dayofyear")
            var_dsets_member_anom.append(member_anom.to_dataset(name=f"{vout}_members"))

            em = ds.mean("M").squeeze()
            if var == "tas":
                tas_field = em[vout]
                finite_tas = tas_field.where(np.isfinite(tas_field), drop=True)
                if finite_tas.size == 0:
                    print(f"  - skipping {group}-{chosen_model_name} tas: no finite realtime values")
                    continue
                tas_median = float(finite_tas.median().values)
                # Guardrail: SubC tas should be in a physically plausible Kelvin range.
                # If values are far outside, skip the model-variable to avoid misleading maps.
                if tas_median < 180.0 or tas_median > 330.0:
                    print(
                        f"  - skipping {group}-{chosen_model_name} tas: implausible median "
                        f"{tas_median:.2f} K (expected ~180-330 K)"
                    )
                    continue
            anom = em[vout] - clim[var]
            if "dayofyear" in anom.dims:
                anom = anom.drop_dims("dayofyear")
            anom = anom.to_dataset(name=vout)
            var_dsets_anom.append(anom)
            var_dsets_full.append(ds)

        if var_dsets_anom:
            ds_model_anom = xr.merge(var_dsets_anom, compat="override").assign_coords(model=model_id_local)
            _debug_print(f"  [DBG] {model_id_local} anomaly vars: {list(ds_model_anom.data_vars)}")
            ds_anoms_by_model.append(ds_model_anom)
        if var_dsets_member_anom:
            ds_model_member_anom = xr.merge(var_dsets_member_anom, compat="override").assign_coords(model=model_id_local)
            _debug_print(f"  [DBG] {model_id_local} member anomaly vars: {list(ds_model_member_anom.data_vars)}")
            ds_member_anoms_by_model.append(ds_model_member_anom)
        if var_dsets_full:
            ds_full_by_model.append(xr.merge(var_dsets_full, compat="override").assign_coords(model=model_id_local))

    if not ds_anoms_by_model:
        if args.allow_empty_input:
            manifest = {
                "fcstdate": fcstdate,
                "models": [],
                "n_week": 0,
                "n_vars": 0,
                "out_images_dir": out_images,
                "out_data_dir": out_data,
                "smoke_mode": True,
                "note": "No realtime inputs found; products stage completed in smoke mode.",
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }
            save_manifest(os.path.join(out_data, "manifest.json"), manifest)
            print("[WARN] No model anomalies assembled; completed in smoke mode due to --allow-empty-input.")
            return
        raise RuntimeError("No model anomalies assembled for this forecast date.")

    # ---- Combine → weeks → SUBC‑MME ----
    ds_models = safe_concat(ds_anoms_by_model, dim='model')
    _debug_print(f"[DBG] ds_models dims: {dict(ds_models.sizes)}")
    _debug_print(f"[DBG] ds_models vars: {list(ds_models.data_vars)}")
    ds_week   = weekly_reduce(ds_models, fcstdate, nweeks=4)

    ds_week_members = None
    if ds_member_anoms_by_model:
        ds_models_members = safe_concat(ds_member_anoms_by_model, dim='model')
        _debug_print(f"[DBG] ds_models_members dims: {dict(ds_models_members.sizes)}")
        _debug_print(f"[DBG] ds_models_members vars: {list(ds_models_members.data_vars)}")
        ds_week_members = weekly_reduce(ds_models_members, fcstdate, nweeks=4)
    
    if int(ds_week.sizes.get('model', 0)) > 1:
        ds_mme = ds_week.mean('model').assign_coords(
            S=np.datetime64(pd.Timestamp(fcstdate), 'ns'),
            model='SUBC-MME',
        )
        ds_subx = xr.concat([ds_week, ds_mme], dim='model')
    else:
        ds_subx = ds_week

    if 'model' in ds_subx.coords:
        init_date_vals: list[str] = []
        nens_vals: list[int] = []
        mme_nens = int(sum(model_ensemble_counts.values())) if model_ensemble_counts else 0
        for mid in ds_subx['model'].values:
            model_name = str(mid)
            if model_name == 'SUBC-MME':
                init_date_vals.append(pd.to_datetime(fcstdate).strftime('%Y%m%d'))
                nens_vals.append(mme_nens)
            else:
                init_ts = model_init_dates.get(model_name)
                init_date_vals.append(init_ts.strftime('%Y%m%d') if init_ts is not None else 'Unknown')
                nens_vals.append(int(model_ensemble_counts.get(model_name, 0)))
        ds_subx = ds_subx.assign_coords(
            init_date=('model', np.asarray(init_date_vals, dtype=object)),
            nens=('model', np.asarray(nens_vals, dtype=np.int32)),
        )
    
    # Clean up attributes
    for k in ("week_start", "week_end"):
        if k in ds_subx.attrs:
            # convert to ISO strings 
            ds_subx.attrs[k] = [pd.Timestamp(x).isoformat() for x in ds_subx.attrs[k]]

    # ---- Save weekly NetCDF ----
    if args.save:
        nc_out = os.path.join(out_data, f"subx_mme_anoms_wk_1-4_{fcstdate}.nc")
        ds_save = ds_subx
        if ds_week_members is not None and ds_week_members.data_vars:
            ds_save = xr.merge([ds_subx, ds_week_members], compat="override")

        mean_vars = sorted([str(v) for v in ds_subx.data_vars])
        member_vars = sorted([str(v) for v in ds_save.data_vars if str(v).endswith("_members")])
        ds_save.attrs["anomaly_mean_variables"] = ",".join(mean_vars)
        ds_save.attrs["anomaly_member_variables"] = ",".join(member_vars)
        ds_save.attrs["anomaly_member_suffix"] = "_members"

        ds_save.to_netcdf(nc_out)
        print(f"[SAVE] {nc_out}")
        _write_backward_compat_pr_anom_files(ds_save, out_data, fcstdate)

    panel_models = [f"{m['group']}-{m['name']}" for m in cfg.get("models", [])]
    panel_models.append("SUBC-MME")
    _plot_legacy_weekly_products(ds_subx, out_images, out_data, fcstdate, panel_models, cfg)

    # ---- Exceedance for all available models (pooled into a single MME map per product) ----
    if args.skip_exceedance:
        print("[INFO] Skipping exceedance computation (--skip-exceedance)")
    elif ds_full_by_model:
        ds_full = safe_concat(ds_full_by_model, dim='model')

        available_models = [str(m) for m in ds_full['model'].values if str(m) != 'SUBC-MME']

        # Reverse map: local model_id -> server/configured model name
        # Needed when model_name_map renames a model (e.g. GEOS_V2p1_5daily -> GEOS_V2p1)
        # but threshold files use the original configured name.
        server_name_by_local: dict[str, str] = {}
        for m_cfg in cfg.get("models", []):
            g, srv = m_cfg["group"], m_cfg["name"]
            srv_key = f"{g}-{srv}"
            local_name = model_map.get(srv_key, model_map.get(srv, srv))
            local_key = f"{g}-{local_name}"
            if local_key != srv_key:
                server_name_by_local[local_key] = srv

        exceedance_configs = cfg['exceedance']
        if isinstance(exceedance_configs, dict):
            exceedance_configs = [exceedance_configs]  # tolerate old single-mapping shape

        for ex in exceedance_configs:
            var = ex['var']
            pct = int(ex['percentile'])
            win = int(ex['window_days'])
            lev = ex.get('lev', 'sfc')
            max_fallback_days = int(ex.get('max_fallback_days', 7))
            direction = ex.get('direction', 'above')
            aggregate = ex.get('aggregate', 'any')
            # Uniquely tags this product's output filenames so multiple products for
            # the same var (e.g. pr above-95 vs pr below-5) never collide/overwrite.
            product_tag = f"p{pct}_{direction}"

            if var not in ds_full:
                print(f"[WARN] Exceedance skipped for {product_tag} (variable '{var}' not present in model dataset).")
                continue

            mmdd = pd.Timestamp(fcstdate).strftime('%m%d')
            processed_models = []
            mme_counts_by_region: dict[str, xr.DataArray] = {}
            mme_members_by_region: dict[str, int] = {}
            _debug_print(
                f"[INFO] Exceedance threshold lookup convention ({product_tag}): "
                f"<hc_root>/{var}{lev}/daily/percentiles/{{model}}-{{group}}/{var}_{{model}}-{{group}}/"
                f"{var}_{{group}}-{{model}}-MMDD.{pct}p.nc"
            )

            for mid in available_models:
                if '-' not in mid:
                    print(f"[WARN] Skipping exceedance for malformed model id '{mid}'")
                    continue

                group, model = mid.split('-', 1)
                # Also try the original server/configured model name if it was remapped
                model_candidates_thr = _unique([model, server_name_by_local.get(mid, model)])

                thr_path = None
                thr_dir = None
                for model_thr in model_candidates_thr:
                    candidate_dir = os.path.join(
                        hc_root,
                        f"{var}{lev}",
                        "daily",
                        "percentiles",
                        f"{model_thr}-{group}",
                        f"{var}_{model_thr}-{group}",
                    )
                    if os.path.isdir(candidate_dir):
                        found = nearest_mmdd_threshold(
                            candidate_dir,
                            var,
                            group,
                            model_thr,
                            mmdd,
                            pct,
                            max_offset_days=max_fallback_days,
                        )
                        if found:
                            thr_path = found
                            thr_dir = candidate_dir
                            break
                    thr_dir = candidate_dir  # keep last tried for warning message

                if not thr_path:
                    candidate_pattern = os.path.join(thr_dir, f"{var}_{group}-{model_candidates_thr[-1]}_*.{pct}p.nc")
                    has_candidates = bool(glob.glob(candidate_pattern))
                    if has_candidates:
                        print(f"[WARN] No threshold within +/-{max_fallback_days} days for {mid} ({product_tag}); skipping model.")
                    else:
                        print(f"[WARN] Missing threshold files for {mid} ({product_tag}) under: {thr_dir}")
                    continue
                if f"_{mmdd}." not in os.path.basename(thr_path):
                    _debug_print(f"[INFO] Using nearest threshold for {mid} ({product_tag}): {os.path.basename(thr_path)}")

                for r in cfg.get('regions', []):
                    region_name = r['name']
                    bounds = r.get('subx', {})
                    lon_slice = tuple(bounds.get('lon', [])) if 'lon' in bounds else None
                    lat_slice = tuple(bounds.get('lat', [])) if 'lat' in bounds else None

                    field = ds_full[var].sel(model=mid)
                    counts = compute_exceedance(
                        field,
                        threshold_path=thr_path,
                        init_date=pd.Timestamp(fcstdate),
                        window=win,
                        percentile=pct,
                        var=var,
                        lon_slice=lon_slice,
                        lat_slice=lat_slice,
                        return_counts=True,
                        direction=direction,
                        aggregate=aggregate,
                    )
                    ens_dim = 'M' if 'M' in field.dims else ('member' if 'member' in field.dims else None)
                    model_nens = int(field.sizes.get(ens_dim, 0)) if ens_dim else 1

                    if region_name in mme_counts_by_region:
                        prev_counts, new_counts = xr.align(mme_counts_by_region[region_name], counts, join='inner')
                        mme_counts_by_region[region_name] = (prev_counts + new_counts)
                        mme_members_by_region[region_name] += model_nens
                    else:
                        mme_counts_by_region[region_name] = counts
                        mme_members_by_region[region_name] = model_nens

                processed_models.append(mid)

            if not processed_models:
                print(f"[WARN] Exceedance skipped for {product_tag} (no model-specific threshold files found).")
                continue

            week0 = pd.Timestamp(fcstdate) + pd.Timedelta(days=2)  # Saturday
            for region_name, mme_counts in mme_counts_by_region.items():
                mme_nens = int(mme_members_by_region.get(region_name, 0))
                if mme_nens <= 0:
                    continue
                mme_probs = (mme_counts / float(mme_nens)) * 100.0
                mme_probs.name = f"{var}_exceed_prob"

                for week_num in [1, 2, 3, 4]:
                    wk_start = week0 + pd.Timedelta(days=(week_num - 1) * 7)
                    wk_end = wk_start + pd.Timedelta(days=6)

                    probs_week = _select_week_window_from_probs(
                        mme_probs,
                        wk_end,
                        week_num,
                        region_name,
                    )
                    if "time_window" in probs_week.coords and "time_window" not in probs_week.dims:
                        probs_week = probs_week.drop_vars("time_window", errors="ignore")
                    out_week_file = os.path.join(
                        out_data,
                        f"exceed_SUBC-MME_{var}_{product_tag}_{region_name}_{fcstdate}_wk{week_num}.nc",
                    )
                    probs_week.to_netcdf(out_week_file)

                    _plot_exceedance_mme_map(
                        out_images=out_images,
                        fcstdate=fcstdate,
                        region_name=region_name,
                        var=var,
                        probs=mme_probs,
                        pct=pct,
                        direction=direction,
                        aggregate=aggregate,
                        product_tag=product_tag,
                        week_num=week_num,
                        week_start=wk_start,
                        week_end=wk_end,
                        global_projection=(region_name == "Global"),
                        mme_member_count=mme_nens,
                    )

    # ---- Manifest ----
    manifest = {
        "fcstdate": fcstdate,
        "models":   list(map(str, ds_subx['model'].values)),
        "n_week":   int(ds_subx['week'][-1].values),
        "n_vars":   len(ds_subx.data_vars),
        "out_images_dir": out_images,
        "out_data_dir":   out_data,
        "timestamp": datetime.utcnow().isoformat()+"Z",
    }
    save_manifest(os.path.join(out_data, "manifest.json"), manifest)
    print("[DONE] Forecast stage complete.")


if __name__ == '__main__':
    main()
