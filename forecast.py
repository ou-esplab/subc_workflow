#!/usr/bin/env python3
# coding: utf-8
"""
forecast.py – SubX weekly anomalies/MME + generic exceedance

What this does
--------------
1) Loads realtime SubX forecast files for each configured model/variable.
2) For each model, picks the most recent IC within the "forecast week" (Fri–Thu).
3) Converts ensemble members to an ensemble-mean field and computes anomalies
   against hindcast daily climatology on matching leads.
4) Aggregates to CPC weeks 1–4 (Sat–Fri):
     - precipitation (pr*) is summed after converting to mm/day
     - everything else is averaged
5) Builds SUBC‑MME as the model-mean of weekly anomalies.
6) Computes "exceedance probability" for all available models using
    precomputed percentile fields (per-model, per-MMDD files).
7) Saves:
   - NetCDF of all models + MME weekly anomalies
   - Summary exceedance PNG per configured region
   - manifest.json

Inputs (from config.yaml)
-------------------------
paths:
  rt_root, hc_root, out_weekly, thresholds_template, thresholds_gefs_pr
models:  list of {group,name,vars,levels}
exceedance: {model_id, var, percentile, window_days, modelkey}
regions:    list of { name, subx:{lon,lat}, pycpt:{lon,lat} }
lon_convention: "0_360" or "negpos"

Usage
-----
python3 forecast.py --config config.yaml --fcstdate YYYYMMDD --save
"""

from __future__ import annotations
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
    weekly_reduce, save_manifest, compute_exceedance,
    nearest_mmdd_threshold,
    plot_exceedance_summary,
)


def _remove_stale_exceedance_outputs(out_dir: str, model_id: str, var: str, fcstdate: str, suffix: str) -> None:
    pattern = os.path.join(out_dir, f"exceed_{model_id}_{var}_*_{fcstdate}.{suffix}")
    for path in glob.glob(pattern):
        try:
            os.remove(path)
            print(f"[INFO] Removed stale exceedance output: {path}")
        except OSError as exc:
            print(f"[WARN] Failed to remove stale output {path}: {exc}")


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
        print(f"[INFO] Removed {removed} stale legacy product image(s).")


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
            rec = (init_ts, path, candidate)
            if week_start <= init_ts <= week_end:
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
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature

    available_models = [str(m) for m in ds_subx["model"].values]
    available_set = set(available_models)
    
    # Always show all available models from the dataset, ordered with SUBC-MME last
    real_models = [m for m in available_models if m != "SUBC-MME"]
    if "SUBC-MME" in available_set:
        models = real_models + ["SUBC-MME"]
    else:
        models = real_models

    ncols = 3
    nrows = math.ceil(len(models) / ncols)
    cmap = plt.get_cmap("RdYlBu_r", len(levels) - 1)
    norm = mcolors.BoundaryNorm(levels, cmap.N)

    for week in [1, 2, 3, 4]:
        fig, axes = plt.subplots(
            nrows,
            ncols,
            figsize=(4.7 * ncols, 2.7 * nrows + 1.3),
            subplot_kw={"projection": ccrs.PlateCarree()},
            squeeze=False,
        )
        fig.suptitle(
            f"SubX Week {week} {title_prefix}: Valid week ending "
            f"{pd.Timestamp(fcstdate) + pd.Timedelta(days=(week * 7 + 1)):%b %d}",
            fontsize=15,
            y=0.99,
        )

        mappable = None
        for idx, model_id in enumerate(models):
            row, col = divmod(idx, ncols)
            ax = axes[row][col]
            ax.set_extent([lon_bounds[0], lon_bounds[1], lat_bounds[0], lat_bounds[1]], crs=ccrs.PlateCarree())
            ax.coastlines("110m", linewidth=0.8)
            ax.add_feature(cfeature.BORDERS, linewidth=0.4)
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
            ax.set_title(model_id, fontsize=10)

        for idx in range(len(models), nrows * ncols):
            row, col = divmod(idx, ncols)
            axes[row][col].axis("off")

        if mappable is not None:
            cbar = fig.colorbar(mappable, ax=axes, orientation="horizontal", fraction=0.05, pad=0.08)
            cbar.set_label(units)

        out_png = os.path.join(out_images, f"{filename_prefix}{domain_name}Week{week}.png")
        plt.tight_layout(rect=[0, 0.05, 1, 0.96])
        fig.savefig(out_png, dpi=150, bbox_inches="tight")
        plt.close(fig)
        print(f"[SAVE] {out_png}")

    da_34 = ds_subx[var_name].sel(week=[3, 4]).mean("week")
    fig, axes = plt.subplots(
        nrows,
        ncols,
        figsize=(4.7 * ncols, 2.7 * nrows + 1.3),
        subplot_kw={"projection": ccrs.PlateCarree()},
        squeeze=False,
    )
    fig.suptitle(f"SubX Weeks 3&4 {title_prefix}", fontsize=15, y=0.99)

    mappable = None
    for idx, model_id in enumerate(models):
        row, col = divmod(idx, ncols)
        ax = axes[row][col]
        ax.set_extent([lon_bounds[0], lon_bounds[1], lat_bounds[0], lat_bounds[1]], crs=ccrs.PlateCarree())
        ax.coastlines("110m", linewidth=0.8)
        ax.add_feature(cfeature.BORDERS, linewidth=0.4)
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
        ax.set_title(model_id, fontsize=10)

    for idx in range(len(models), nrows * ncols):
        row, col = divmod(idx, ncols)
        axes[row][col].axis("off")

    if mappable is not None:
        cbar = fig.colorbar(mappable, ax=axes, orientation="horizontal", fraction=0.05, pad=0.08)
        cbar.set_label(units)

    out_png = os.path.join(out_images, f"{filename_prefix}{domain_name}Weeks3&4.png")
    plt.tight_layout(rect=[0, 0.05, 1, 0.96])
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"[SAVE] {out_png}")


def _plot_legacy_weekly_products(ds_subx: xr.Dataset, out_images: str, fcstdate: str, panel_models: list[str]) -> None:
    _plot_weekly_panels(
        ds_subx,
        var_name="tas",
        out_images=out_images,
        fcstdate=fcstdate,
        title_prefix="2m Temperature Anomalies (C)",
        units="C",
        filename_prefix="2mTemp",
        domain_name="NorthAmerica",
        lon_bounds=(-170, -30),
        lat_bounds=(10, 80),
        levels=np.array([-4, -3, -2.5, -2, -1.5, -1, -0.2, 0.2, 0.5, 1, 1.5, 2, 2.5, 3, 4]),
        panel_models=panel_models,
    )

    _plot_weekly_panels(
        ds_subx,
        var_name="pr",
        out_images=out_images,
        fcstdate=fcstdate,
        title_prefix="Precipitation Anomalies (mm/week)",
        units="mm/week",
        filename_prefix="Precip",
        domain_name="Global",
        lon_bounds=(-180, 180),
        lat_bounds=(-60, 80),
        levels=np.array([-35, -25, -15, -10, -5, -2, 2, 5, 10, 15, 25, 35]),
        panel_models=panel_models,
    )
    _plot_weekly_panels(
        ds_subx,
        var_name="pr",
        out_images=out_images,
        fcstdate=fcstdate,
        title_prefix="Precipitation Anomalies (mm/week)",
        units="mm/week",
        filename_prefix="Precip",
        domain_name="NorthAmerica",
        lon_bounds=(-170, -30),
        lat_bounds=(10, 80),
        levels=np.array([-35, -25, -15, -10, -5, -2, 2, 5, 10, 15, 25, 35]),
        panel_models=panel_models,
    )
    _plot_weekly_panels(
        ds_subx,
        var_name="pr",
        out_images=out_images,
        fcstdate=fcstdate,
        title_prefix="Precipitation Anomalies (mm/week)",
        units="mm/week",
        filename_prefix="Precip",
        domain_name="Iran",
        lon_bounds=(40, 64),
        lat_bounds=(24, 40),
        levels=np.array([-35, -25, -15, -10, -5, -2, 2, 5, 10, 15, 25, 35]),
        panel_models=panel_models,
    )
    _plot_weekly_panels(
        ds_subx,
        var_name="pr",
        out_images=out_images,
        fcstdate=fcstdate,
        title_prefix="Precipitation Anomalies (mm/week)",
        units="mm/week",
        filename_prefix="Precip",
        domain_name="Venezuela",
        lon_bounds=(-73, -60),
        lat_bounds=(0, 13),
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
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', required=True, help='Path to config.yaml')
    ap.add_argument('--fcstdate', required=False, help='YYYYMMDD (optional; defaults to latest Thursday)')
    ap.add_argument('--save', action='store_true', help='Write NetCDF + manifest to disk')
    ap.add_argument('--allow-empty-input', action='store_true', help='Exit successfully when no realtime model files are available')
    args = ap.parse_args()

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
    _remove_stale_legacy_products(out_images)

    ds_anoms_by_model = []
    ds_full_by_model  = []

    for m in cfg["models"]:
        group = m["group"]
        server_model = m["name"]
        model_candidates = _candidate_model_names(group, server_model, model_map)
        local_model = model_candidates[0]

        varlist, levlist = m["vars"], m["levels"]

        # Canonical model_id for coords/labels/outputs
        model_id_local = f"{group}-{local_model}"
        print(f"[MODEL] server={group}-{server_model}  local={model_id_local}")

        var_dsets_anom = []
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
                print(
                    f"  - missing this week under candidates: "
                    f"{', '.join(f'{group}-{name}' for name in model_candidates)}"
                )
                continue

            fp, chosen_model_name, chosen_init = chosen
            ds = xr.open_dataset(fp)
            print(f"  [INFO] Using init for {var}: {group}-{chosen_model_name} {chosen_init:%Y%m%d}")

            model_id_local = f"{group}-{chosen_model_name}"

            if "P" in ds[var].dims:
                ds = ds.sel(P=int(plev))
            ds["S"] = ds["S"].dt.floor("D")
            ds = ds.dropna("S", how="all")
            if ds.S.size == 0:
                print(f"  - all missing after drop: {var}")
                continue
            ds = ds.sel(S=ds["S"][-1])

            ds["L"] = ds["S"].values + ds["L"].dt.floor("D")
            ds = ds.drop_vars("S", errors="ignore")
            ds = ds.rename({"X": "lon", "Y": "lat", "L": "lead"})
            ds["lon"].attrs["units"] = "degrees_east"
            ds["lat"].attrs["units"] = "degrees_north"
            ds = ensure_lon(ds, lon_conv).assign_coords(model=model_id_local)   # <-- local id

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

            em = ds.mean("M").squeeze()
            if var == "tas":
                tas_field = em[vout]
                finite_tas = tas_field.where(np.isfinite(tas_field), drop=True)
                if finite_tas.size == 0:
                    print(f"  - skipping {group}-{chosen_model_name} tas: no finite realtime values")
                    continue
                tas_median = float(finite_tas.median().values)
                # Guardrail: SubX tas should be in a physically plausible Kelvin range.
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
            anom = anom.assign_coords(nens=ds.sizes.get('M', 1))

            var_dsets_anom.append(anom)
            var_dsets_full.append(ds)

        if var_dsets_anom:
            ds_model_anom = xr.merge(var_dsets_anom, compat="override").assign_coords(model=model_id_local)
            print(f"  [DBG] {model_id_local} anomaly vars: {list(ds_model_anom.data_vars)}")
            ds_anoms_by_model.append(ds_model_anom)
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
    print("[DBG] ds_models dims:", dict(ds_models.sizes))
    print("[DBG] ds_models vars:", list(ds_models.data_vars))
    ds_week   = weekly_reduce(ds_models, fcstdate, nweeks=4)
    
    if int(ds_week.sizes.get('model', 0)) > 1:
        ds_mme = ds_week.mean('model').assign_coords(
            S=np.datetime64(pd.Timestamp(fcstdate), 'ns'),
            model='SUBC-MME',
            nens=ds_week['nens'].sum(),
            ic_dates=pd.to_datetime(fcstdate).strftime('%Y%m%d'),
        )
        ds_subx = xr.concat([ds_week, ds_mme], dim='model')
    else:
        ds_subx = ds_week
    
    # Clean up attributes
    for k in ("week_start", "week_end"):
        if k in ds_subx.attrs:
            # convert to ISO strings 
            ds_subx.attrs[k] = [pd.Timestamp(x).isoformat() for x in ds_subx.attrs[k]]

    # ---- Save weekly NetCDF ----
    if args.save:
        nc_out = os.path.join(out_data, f"subx_mme_anoms_wk_1-4_{fcstdate}.nc")
        ds_subx.to_netcdf(nc_out)
        print(f"[SAVE] {nc_out}")

    panel_models = [f"{m['group']}-{m['name']}" for m in cfg.get("models", [])]
    panel_models.append("SUBC-MME")
    _plot_legacy_weekly_products(ds_subx, out_images, fcstdate, panel_models)

    # ---- Exceedance for all available models ----
    if ds_full_by_model:
        ds_full = safe_concat(ds_full_by_model, dim='model')

        ex = cfg['exceedance']
        var = ex['var']
        pct = int(ex['percentile'])
        win = int(ex['window_days'])
        lev = ex.get('lev', 'sfc')
        max_fallback_days = int(ex.get('max_fallback_days', 7))

        if var in ds_full:
            mmdd = pd.Timestamp(fcstdate).strftime('%m%d')
            processed_models = []
            available_models = [str(m) for m in ds_full['model'].values if str(m) != 'SUBC-MME']

            for mid in available_models:
                if '-' not in mid:
                    print(f"[WARN] Skipping exceedance for malformed model id '{mid}'")
                    continue

                group, model = mid.split('-', 1)
                # New threshold layout:
                # <hc_root>/{var}{lev}/daily/percentiles/{model}-{group}/{var}_{model}-{group}/{var}_{group}-{model}_{mmdd}.95p.nc
                thr_dir = os.path.join(
                    hc_root,
                    f"{var}{lev}",
                    "daily",
                    "percentiles",
                    f"{model}-{group}",
                    f"{var}_{model}-{group}",
                )

                thr_path = nearest_mmdd_threshold(
                    thr_dir,
                    var,
                    group,
                    model,
                    mmdd,
                    pct,
                    max_offset_days=max_fallback_days,
                )
                if not thr_path:
                    candidate_pattern = os.path.join(thr_dir, f"{var}_{group}-{model}_*.{pct}p.nc")
                    has_candidates = bool(glob.glob(candidate_pattern))
                    if has_candidates:
                        print(f"[WARN] No threshold within +/-{max_fallback_days} days for {mid}; skipping model.")
                    else:
                        print(f"[WARN] Missing threshold files for {mid} under: {thr_dir}")
                    _remove_stale_exceedance_outputs(out_data, mid, var, fcstdate, "nc")
                    _remove_stale_exceedance_outputs(out_images, mid, var, fcstdate, "png")
                    continue
                if f"_{mmdd}." not in os.path.basename(thr_path):
                    print(f"[INFO] Using nearest threshold for {mid}: {os.path.basename(thr_path)}")

                for r in cfg.get('regions', []):
                    region_name = r['name']
                    bounds = r.get('subx', {})
                    lon_slice = tuple(bounds.get('lon', [])) if 'lon' in bounds else None
                    lat_slice = tuple(bounds.get('lat', [])) if 'lat' in bounds else None

                    field = ds_full[var].sel(model=mid).mean('M')
                    probs = compute_exceedance(
                        field,
                        threshold_path=thr_path,
                        init_date=pd.Timestamp(fcstdate),
                        window=win,
                        percentile=pct,
                        var=var,
                        lon_slice=lon_slice,
                        lat_slice=lat_slice,
                    )

                    out_file = os.path.join(out_data, f"exceed_{mid}_{var}_{region_name}_{fcstdate}.nc")
                    probs.to_netcdf(out_file)

                    out_png = os.path.join(out_images, f"exceed_{mid}_{var}_{region_name}_{fcstdate}.png")
                    title = f"Exceedance Prob (avg windows) – {mid} {var} – {region_name} – {fcstdate}"
                    plotted = plot_exceedance_summary(probs, title, out_png)
                    if plotted:
                        print(f"[SAVE] {out_png}")
                    else:
                        print(f"[WARN] Skipped exceedance plot for {mid} {region_name} (non-plottable output).")

                processed_models.append(mid)

            if not processed_models:
                print("[WARN] Exceedance skipped (no model-specific threshold files found).")
        else:
            print(f"[WARN] Exceedance skipped (variable '{var}' not present in model dataset).")

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