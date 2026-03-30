#!/usr/bin/env python3
from __future__ import annotations

import glob
import json
import os
from datetime import datetime

import numpy as np
import pandas as pd
import xarray as xr
import yaml

from utils.subc_pycpt_utils import (
    compute_exceedance,
    ensure_lon,
    fcst_week_dates,
    nearest_mmdd_threshold,
    safe_concat,
)


def main() -> int:
    cfg = yaml.safe_load(open("config.yaml"))
    scratch_dir = ".scratch"
    os.makedirs(scratch_dir, exist_ok=True)
    fcstdate = "20260305"
    week_dates = fcst_week_dates(fcstdate)
    rt_root = cfg["paths"]["rt_root"]
    hc_root = cfg["paths"]["hc_root"]
    lon_conv = cfg.get("lon_convention", "0_360")
    ex = cfg["exceedance"]
    var = ex["var"]
    lev = ex.get("lev", "sfc")
    pct = int(ex["percentile"])
    max_fallback_days = int(ex.get("max_fallback_days", 7))
    mmdd = pd.Timestamp(fcstdate).strftime("%m%d")

    model_map = cfg.get("model_name_map", {}) or {}
    ds_full_by_model = []

    for m in cfg["models"]:
        group = m["group"]
        server_model = m["name"]
        if var not in m["vars"]:
            continue

        local_model = model_map.get(server_model, model_map.get(f"{group}-{server_model}", server_model))
        model_id_local = f"{group}-{local_model}"
        plev = m["levels"][m["vars"].index(var)]

        patt = f"{rt_root}/{group}-{local_model}/forecast/{var}/{var}_{group}-{local_model}_*.daily.nc"
        files = sorted(glob.glob(patt))
        if not files:
            continue

        ds = xr.open_mfdataset(files, combine="by_coords")
        if "P" in ds[var].dims:
            ds = ds.sel(P=int(plev))

        ds["S"] = ds["S"].dt.floor("D")
        ds = ds.sel(S=ds["S"].isin(week_dates.values))
        if ds.S.size == 0:
            continue

        ds = ds.dropna("S", how="all")
        if ds.S.size == 0:
            continue

        ds = ds.sel(S=ds["S"][-1])
        ds["L"] = ds["S"].values + ds["L"].dt.floor("D")
        ds = ds.drop_vars("S", errors="ignore").rename({"X": "lon", "Y": "lat", "L": "lead"})
        ds = ensure_lon(ds, lon_conv).assign_coords(model=model_id_local)
        ds_full_by_model.append(ds.assign_coords(model=model_id_local))

    if not ds_full_by_model:
        print("No available model data for diagnostics")
        return 2

    ds_full = safe_concat(ds_full_by_model, dim="model")

    rows = []
    for mid in [str(x) for x in ds_full["model"].values if str(x) != "SUBC-MME"]:
        if "-" not in mid:
            continue
        group, model = mid.split("-", 1)

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

        for r in cfg.get("regions", []):
            region_name = r["name"]
            bounds = r.get("subx", {})
            lon_slice = tuple(bounds.get("lon", [])) if "lon" in bounds else None
            lat_slice = tuple(bounds.get("lat", [])) if "lat" in bounds else None

            row = {
                "model": mid,
                "region": region_name,
                "threshold_path": thr_path,
            }

            if not thr_path:
                candidate_pattern = os.path.join(thr_dir, f"{var}_{group}-{model}_*.{pct}p.nc")
                row["status"] = (
                    f"no_threshold_within_{max_fallback_days}_days"
                    if glob.glob(candidate_pattern)
                    else "missing_threshold"
                )
                rows.append(row)
                continue

            field = ds_full[var].sel(model=mid).mean("M")
            if lon_slice:
                field = field.sel(lon=slice(*lon_slice))
            if lat_slice:
                field = field.sel(lat=slice(*lat_slice))

            probs = compute_exceedance(
                field,
                threshold_path=thr_path,
                init_date=pd.Timestamp(fcstdate),
                window=int(ex["window_days"]),
                percentile=pct,
                var=var,
                lon_slice=None,
                lat_slice=None,
            )
            summary = probs.mean("time_window") if "time_window" in probs.dims else probs

            row.update(
                {
                    "field_sizes": dict(field.sizes),
                    "probs_sizes": dict(probs.sizes),
                    "summary_sizes": dict(summary.sizes),
                    "summary_dims": list(summary.dims),
                    "is_2d_map": list(summary.dims) == ["lat", "lon"],
                    "finite": int(np.isfinite(summary.values).sum()),
                    "total": int(summary.size),
                }
            )
            rows.append(row)

    out = os.path.join(scratch_dir, "exceed_diag.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2)

    print(f"wrote {out}")
    for row in rows:
        print(
            f"{row['model']} | {row['region']} | "
            f"status={row.get('status', 'ok')} | "
            f"2d={row.get('is_2d_map')} | "
            f"summary_dims={row.get('summary_dims')} | "
            f"summary_sizes={row.get('summary_sizes')}"
        )

    bad = [r for r in rows if r.get("is_2d_map") is False]
    print(f"non_2d_cases={len(bad)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
