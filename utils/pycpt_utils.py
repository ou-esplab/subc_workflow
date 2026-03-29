#!/usr/bin/env python3
# coding: utf-8


# ---- Seasonal PyCPT helpers (NMME & SubC shared) ----
import xarray as xr
import numpy as np

def standardize_mos_prob(mos_prob: xr.DataArray) -> xr.DataArray:
    if "T" in mos_prob.dims:
        mos_prob = mos_prob.isel(T=0)
    if "C" not in mos_prob.dims:
        raise ValueError(f"Expected dim 'C' in MOS probabilistic, got dims={mos_prob.dims}")
    out = mos_prob.rename({"C": "cat"}).assign_coords(cat=["bn", "nn", "an"]).sel(cat=["bn","nn","an"])
    if float(out.max()) <= 1.01:
        out = out * 100.0
    return out

def hindcast_terciles_from_anoms(hc_anom: xr.DataArray):
    if "T" not in hc_anom.dims:
        raise ValueError(f"Expected 'T' in hindcast dims, got {hc_anom.dims}")
    t33 = hc_anom.quantile(0.33, dim="T"); t66 = hc_anom.quantile(0.66, dim="T")
    for v in (t33, t66):
        if "quantile" in v.dims: v = v.isel(quantile=0, drop=True)
        if "quantile" in v.coords: v = v.reset_coords("quantile", drop=True)
    return t33, t66

def raw_onehot_terciles_percent(fc_anom: xr.DataArray, t33: xr.DataArray, t66: xr.DataArray) -> xr.DataArray:
    if "T" in fc_anom.dims:
        fc_anom = fc_anom.isel(T=-1)
    bn = (fc_anom < t33).astype(float) * 100.0
    an = (fc_anom >= t66).astype(float) * 100.0
    nn = 100.0 - bn - an
    return xr.concat([bn, nn, an], dim="cat").assign_coords(cat=["bn","nn","an"])

def _latlon_names(da: xr.DataArray):
    lat_candidates = ["lat", "latitude", "Y"]; lon_candidates = ["lon", "longitude", "X"]
    lat = next((c for c in lat_candidates if c in da.coords), None)
    lon = next((c for c in lon_candidates if c in da.coords), None)
    if lat is None or lon is None:
        raise ValueError(f"Could not infer lat/lon in coords={list(da.coords)}")
    return lat, lon

def interp_to_target_grid_safe(src: xr.DataArray, target: xr.DataArray) -> xr.DataArray:
    if "cat" in target.dims:
        target2 = target.isel(cat=0, drop=True)
    else:
        target2 = target
    tlat, tlon = _latlon_names(target2)
    if "cat" in src.dims:
        outs = []
        for c in src["cat"].values:
            sl = src.sel(cat=c)
            slat, slon = _latlon_names(sl)
            oc = sl.interp({slat: target2[tlat], slon: target2[tlon]}, method="linear").expand_dims(cat=[c])
            outs.append(oc)
        return xr.concat(outs, dim="cat").assign_coords(cat=src["cat"].values)
    slat, slon = _latlon_names(src)
    return src.interp({slat: target2[tlat], slon: target2[tlon]}, method="linear")

def _select_forecast_slice(da: xr.DataArray) -> xr.DataArray:
    if "target" in da.dims: da = da.isel(target=0)
    if "T" in da.dims:       da = da.isel(T=-1)
    if "time" in da.dims:    da = da.isel(time=-1)
    return da

def _hindcast_climatology(hc: xr.DataArray) -> xr.DataArray:
    if "target" in hc.dims: hc = hc.isel(target=0)
    if "T" in hc.dims:      return hc.mean("T")
    if "time" in hc.dims:   return hc.mean("time")
    raise ValueError(f"Can't find time/year dim in hindcast. dims={hc.dims}")

def compute_raw_anom(forecast_da: xr.DataArray, hindcast_da: xr.DataArray) -> xr.DataArray:
    return _select_forecast_slice(forecast_da) - _hindcast_climatology(hindcast_da)

def safe_tag(name: str) -> str:
    return name.replace(".", "_").replace("/", "_").replace(" ", "_")
