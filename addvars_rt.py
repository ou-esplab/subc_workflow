#!/usr/bin/env python3
"""Append new SubX realtime dates into Arraylake groups.

This script is intentionally config-driven so it can be invoked by the
workflow runner without hard-coded paths or credentials.
"""

from __future__ import annotations

import argparse
import glob
import os
import time
from datetime import datetime
from typing import Dict, List, Sequence

import numpy as np
import xarray as xr
import yaml


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def resolve_model_source_names(cfg: dict, model: dict) -> List[str]:
    group = model["group"]
    server_name = model["name"]
    model_map = cfg.get("model_name_map") or {}
    mapped = model_map.get(f"{group}-{server_name}", model_map.get(server_name, server_name))

    names = [mapped]
    if server_name not in names:
        names.append(server_name)
    return names


def build_file_list(input_root: str, group: str, model_names: Sequence[str], variable: str) -> List[str]:
    files: List[str] = []
    for model_name in model_names:
        pattern = (
            f"{input_root}/{group}-{model_name}/forecast/{variable}/"
            f"{variable}_{group}-{model_name}_????????.daily.nc"
        )
        files.extend(glob.glob(pattern))
    return sorted(set(files))


def clean_dataset(ds: xr.Dataset) -> xr.Dataset:
    chunk_dict = {dim: int(ds.sizes[dim]) for dim in ("L", "Y", "X") if dim in ds.sizes}
    chunk_dict.update({"M": 1, "S": 1})
    if "P" in ds.sizes:
        chunk_dict["P"] = 1

    ds = ds.chunk(chunk_dict)
    for var in ds.data_vars:
        ds[var].attrs.pop("missing_value", None)
        ds[var].attrs.pop("_FillValue", None)
        ds[var].encoding = {}

    ds = ds.sortby("S")
    unique_s = np.unique(ds["S"].values)
    ds = ds.sel(S=unique_s)
    return ds


def parse_fcstdate(fcstdate: str | None) -> np.datetime64 | None:
    if not fcstdate:
        return None
    parsed = datetime.strptime(fcstdate, "%Y%m%d")
    return np.datetime64(parsed.date().isoformat())


def select_target_s(ds: xr.Dataset, target_s: np.datetime64 | None) -> xr.Dataset:
    if target_s is None:
        return ds

    s_values = ds["S"].values
    s_days = s_values.astype("datetime64[D]")
    keep = s_days == target_s
    if not np.any(keep):
        return ds.isel(S=slice(0, 0))
    return ds.isel(S=np.where(keep)[0])


def main() -> int:
    ap = argparse.ArgumentParser(description="Append SubX realtime data into Arraylake")
    ap.add_argument("--config", default="config.yaml", help="Workflow config path")
    ap.add_argument("--date", default=None, help="Optional start date filter YYYYMMDD")
    args = ap.parse_args()

    cfg = load_config(os.path.abspath(args.config))
    al_cfg = cfg.get("arraylake") or {}

    if not bool(al_cfg.get("enabled", False)):
        print("[arraylake] Disabled in config; skipping.")
        return 0

    try:
        from arraylake import Client
    except Exception as exc:
        raise RuntimeError("arraylake package is required when arraylake.enabled=true") from exc

    repo_name = al_cfg.get("repo", "ou-subc/subc-forecasts")
    branch = al_cfg.get("branch", "main")
    input_root = al_cfg.get("input_root") or (cfg.get("paths") or {}).get("rt_root")
    if not input_root:
        raise RuntimeError("arraylake.input_root (or paths.rt_root) must be configured")

    default_variables = ["pr", "tas", "rlut", "ts", "ua", "va", "zg"]
    variables = al_cfg.get("variables") or default_variables
    batch_size = int(al_cfg.get("batch_size", 1))

    token = os.environ.get("ARRAYLAKE_TOKEN") or al_cfg.get("token")
    if token:
        client = Client(token=token)
    else:
        client = Client()

    target_s = parse_fcstdate(args.date)

    models = cfg.get("models") or []
    if not models:
        raise RuntimeError("No models configured in config.yaml")

    repo = client.get_repo(repo_name)

    for model in models:
        group = model["group"]
        server_model = model["name"]
        source_names = resolve_model_source_names(cfg, model)

        group_name = f"{group.lower()}-{server_model.lower()}-forecast"
        print(f"\n[arraylake] Processing {group_name}")

        ds_list: List[xr.Dataset] = []
        for variable in variables:
            files = build_file_list(input_root, group, source_names, variable)
            if not files:
                print(f"[arraylake] No files found for {group}/{server_model}/{variable}")
                continue

            print(f"[arraylake] Reading {len(files)} files for {variable}")
            ds = xr.open_mfdataset(
                files,
                engine="netcdf4",
                decode_timedelta=True,
                concat_dim="S",
                combine="nested",
            )
            ds_list.append(ds)

        if not ds_list:
            print("[arraylake] No variables found; skipping model")
            continue

        ds_all = clean_dataset(xr.merge(ds_list))
        ds_all = select_target_s(ds_all, target_s)
        if ds_all.sizes.get("S", 0) == 0:
            print("[arraylake] No matching S for requested date; skipping model")
            continue

        session = repo.readonly_session(branch)
        store = session.store

        try:
            ds_existing = xr.open_zarr(store, group=group_name, consolidated=False)
            existing_s = np.unique(ds_existing["S"].values.astype("datetime64[D]"))
        except Exception:
            print(f"[arraylake] Group {group_name} not found in repo; skipping")
            continue

        incoming_s = np.unique(ds_all["S"].values.astype("datetime64[D]"))
        new_s = np.setdiff1d(incoming_s, existing_s)
        if len(new_s) == 0:
            print("[arraylake] No new start dates to append")
            continue

        vars_existing = set(ds_existing.data_vars)
        vars_incoming = set(ds_all.data_vars)
        write_vars = sorted(vars_existing.intersection(vars_incoming))
        if not write_vars:
            print("[arraylake] No overlapping variables with existing group; skipping")
            continue

        print(f"[arraylake] Appending {len(new_s)} new start date(s): {new_s}")

        for i in range(0, len(new_s), batch_size):
            batch_s = new_s[i : i + batch_size]
            ds_write = ds_all[write_vars].sel(S=batch_s).reset_coords(drop=True).chunk({"S": 1})
            for var in ds_write.data_vars:
                ds_write[var].encoding = {}

            attempts = 0
            while True:
                attempts += 1
                try:
                    with repo.transaction(
                        branch,
                        message=f"Append {group_name} S={batch_s}",
                    ) as tx_store:
                        ds_write.to_zarr(
                            tx_store,
                            group=group_name,
                            mode="a",
                            append_dim="S",
                            zarr_format=3,
                            consolidated=False,
                            align_chunks=True,
                        )
                    break
                except Exception as exc:
                    if attempts >= 3:
                        raise RuntimeError(
                            f"Failed to append batch for {group_name} after {attempts} attempts"
                        ) from exc
                    time.sleep(2)

    print("\n[arraylake] Completed append workflow")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
