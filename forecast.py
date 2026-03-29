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
6) Computes "exceedance probability" for a chosen model/variable using a
   precomputed percentile field (generic template; currently defaulting to GEFS).
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

from subc_pycpt_utils import (
    latest_thursday, fcst_week_dates, ensure_lon, safe_concat,
    weekly_reduce, save_manifest, compute_exceedance, resolve_threshold_path,
    plot_exceedance_summary,
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

    out_images = os.path.join(out_weekly, fcstdate, 'images')
    out_data   = os.path.join(out_weekly, fcstdate, 'data')
    os.makedirs(out_images, exist_ok=True)
    os.makedirs(out_data,   exist_ok=True)

    ds_anoms_by_model = []
    ds_full_by_model  = []

    for m in cfg["models"]:
        group = m["group"]
        server_model = m["name"]
        local_model  = model_map.get(server_model, server_model)   # <-- USE THIS LOCALLY
        local_model  = model_map.get(server_model, model_map.get(f"{group}-{server_model}", server_model))

        varlist, levlist = m["vars"], m["levels"]

        # Canonical model_id for coords/labels/outputs
        model_id_local = f"{group}-{local_model}"
        print(f"[MODEL] server={group}-{server_model}  local={model_id_local}")

        var_dsets_anom = []
        var_dsets_full = []

        for var, plev in zip(varlist, levlist):
            patt = f"{rt_root}/{group}-{local_model}/forecast/{var}/{var}_{group}-{local_model}_*.daily.nc"
            files = sorted(glob.glob(patt))
            if not files:
                print(f"  - no files matched {patt}")
                continue

            ds = xr.open_mfdataset(files, combine='by_coords')

            if "P" in ds[var].dims:
                ds = ds.sel(P=int(plev))
            ds["S"] = ds["S"].dt.floor("D")
            ds = ds.sel(S=ds["S"].isin(week_dates.values))
            if ds.S.size == 0:
                print(f"  - missing this week: {var}")
                continue
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

            # ---- hc_root climo: use local_model ----
            mmdd_last   = pd.to_datetime(week_dates[-1]).strftime("%m%d")
            climo_fname = (
                f"{hc_root}/{var}{plev}/daily/climo/{group}-{local_model}/"
                f"{var}_{group}-{local_model}_{mmdd_last}.climo.p.nc"
            )
            if not os.path.exists(climo_fname):
                print(f"  - missing climatology {climo_fname}")
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
    print("[DBG] ds_models dims:", dict(ds_models.dims))
    print("[DBG] ds_models vars:", list(ds_models.data_vars))
    ds_week   = weekly_reduce(ds_models, fcstdate, nweeks=4)
    
    ds_mme = ds_week.mean('model').assign_coords(
        S=np.datetime64(pd.to_datetime(fcstdate)),
        model='SUBC-MME',
        nens=ds_week['nens'].sum(),
        ic_dates=pd.to_datetime(fcstdate).strftime('%Y%m%d'),
    )

    ds_subx = xr.concat([ds_week, ds_mme], dim='model')
    
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

    # ---- Exceedance (generic; currently thresholds available for GEFS) ----
    if ds_full_by_model:
        ds_full = safe_concat(ds_full_by_model, dim='model')

        ex  = cfg['exceedance']
        mid = ex['model_id']     # e.g., EMC-GEFSv12_CPC
        var = ex['var']          # e.g., pr
        pct = ex['percentile']
        win = ex['window_days']

        thr_path = resolve_threshold_path(
            template      = cfg['paths'].get('thresholds_template', ''),
            percentile    = pct,
            modelkey      = ex.get('modelkey', 'GEFSthres'),
            var           = var,
            gefs_fallback = cfg['paths'].get('thresholds_gefs_pr'),
        )

        if thr_path and (var in ds_full) and (mid in ds_full['model'].values):
            for r in cfg.get('regions', []):                           # unified regions
                region_name = r['name']
                bounds      = r.get('subx', {})                       # use subx extents here
                lon_slice   = tuple(bounds.get('lon', [])) if 'lon' in bounds else None
                lat_slice   = tuple(bounds.get('lat', [])) if 'lat' in bounds else None

                field = ds_full[var].sel(model=mid).mean('M')          # ensemble mean field
                print(field)
                probs = compute_exceedance(
                    field, threshold_path=thr_path,
                    init_date=pd.Timestamp(fcstdate),
                    window=win, percentile=pct, var=var,
                    lon_slice=lon_slice, lat_slice=lat_slice,
                )
                
                # Write probabilities to file
                out_file=os.path.join(out_data, f"exceed_{mid}_{var}_{region_name}_{fcstdate}.nc")
                probs.to_netcdf(out_file)
                
                # summary plot (mean across windows)
                out_png = os.path.join(out_images, f"exceed_{mid}_{var}_{region_name}_{fcstdate}.png")
                title   = f"Exceedance Prob (avg windows) – {mid} {var} – {region_name} – {fcstdate}"
                plot_exceedance_summary(probs, title, out_png)
                print(f"[SAVE] {out_png}")
        else:
            print("[WARN] Exceedance skipped (no thresholds path or model/var absent).")

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