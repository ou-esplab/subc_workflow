#!/usr/bin/env python3
"""Preflight validation for realtime SubX inputs.

The forecast stage depends on daily realtime files already being present on disk.
This validator checks that each configured model-variable combination has at least
one matching file for the requested forecast date and writes a JSON manifest that
documents what was found.
"""

import argparse
import glob
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

import yaml


def latest_thursday(date_str=None):
    if date_str:
        current = datetime.strptime(date_str, "%Y%m%d")
    else:
        current = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return (current - timedelta(days=(current.weekday() - 3) % 7)).strftime("%Y%m%d")


def load_config(path):
    with open(path, "r", encoding="utf-8") as config_file:
        return yaml.safe_load(config_file) or {}


def resolve_local_model(group, server_model, mapping):
    return mapping.get("{}-{}".format(group, server_model), mapping.get(server_model, server_model))


def _date_window(fcstdate, lookback_days):
    end = datetime.strptime(fcstdate, "%Y%m%d")
    days = max(0, lookback_days - 1)
    return {(end - timedelta(days=offset)).strftime("%Y%m%d") for offset in range(days + 1)}


def _newest_match(pattern, allowed_dates):
    # Match the date token in names like var_GROUP-MODEL_YYYYMMDD.daily.nc
    date_token = re.compile(r"_(\d{8})(?:[^0-9].*)?\.daily\.nc$")
    candidates = []
    for path in glob.glob(pattern):
        match = date_token.search(os.path.basename(path))
        if not match:
            continue
        date_str = match.group(1)
        if date_str in allowed_dates:
            candidates.append((date_str, path))

    if not candidates:
        return []

    newest = max(candidates, key=lambda item: item[0])[1]
    return [newest]


def _validation_record(group, server_model, local_model, variable, pattern, matches):
    return {
        "group": group,
        "server_model": server_model,
        "local_model": local_model,
        "variable": variable,
        "pattern": pattern,
        "matches": matches,
        "ok": bool(matches),
    }


def iter_records(config, fcstdate, lookback_days, rt_root=None):
    rt_root = rt_root or (config.get("paths") or {}).get("rt_root")
    if not rt_root:
        raise ValueError("config.paths.rt_root is required")

    model_map = config.get("model_name_map") or {}
    models = config.get("models") or []
    if not models:
        raise ValueError("config.models must contain at least one model entry")

    allowed_dates = _date_window(fcstdate, lookback_days)

    for model in models:
        group = model["group"]
        server_model = model["name"]
        local_model = resolve_local_model(group, server_model, model_map)
        for variable in model.get("vars") or []:
            pattern = os.path.join(
                rt_root,
                "{}-{}".format(group, local_model),
                "forecast",
                variable,
                "{}_{}-{}_*.daily.nc".format(variable, group, local_model),
            )
            yield _validation_record(
                group=group,
                server_model=server_model,
                local_model=local_model,
                variable=variable,
                pattern=pattern,
                matches=_newest_match(pattern, allowed_dates),
            )


def _schema_snapshot(path):
    try:
        import xarray as xr
    except Exception as exc:
        return {}, "xarray import failed: {}".format(exc)

    try:
        with xr.open_dataset(path, decode_times=False) as ds:
            snapshot = {
                "dims": {name: int(size) for name, size in ds.dims.items()},
                "coords": sorted(list(ds.coords.keys())),
                "data_vars": sorted(list(ds.data_vars.keys())),
            }
            return snapshot, None
    except Exception as exc:
        return {}, "open_dataset failed: {}".format(exc)


def _dim_role(name):
    token = str(name).strip().lower()
    if token in {"time", "t", "l"}:
        return "time"
    if token in {"lat", "latitude", "y"}:
        return "lat"
    if token in {"lon", "longitude", "x"}:
        return "lon"
    if token in {"m", "member", "members", "ens", "ensemble"}:
        return "member"
    if token in {"s", "start", "init", "initialization"}:
        return "init"
    if token in {"p", "lev", "lev_p", "level", "levels", "plev"}:
        return "level"
    return token


def _core_dim_sizes(snapshot):
    dims = snapshot.get("dims") or {}
    out = {}
    for dim_name, size in dims.items():
        role = _dim_role(dim_name)
        if role in {"time", "lat", "lon"}:
            out[role] = int(size)
    return out


def _contains_variable(snapshot, variable):
    vars_list = snapshot.get("data_vars") or []
    v = str(variable).strip().lower()
    for name in vars_list:
        token = str(name).strip().lower()
        if token == v or token.startswith(v + "_") or token.endswith("_" + v) or v in token:
            return True
    return False


def _schemas_compatible(primary_schema, shadow_schema, variable):
    primary_core = _core_dim_sizes(primary_schema)
    shadow_core = _core_dim_sizes(shadow_schema)

    if set(primary_core.keys()) != {"time", "lat", "lon"}:
        return False
    if set(shadow_core.keys()) != {"time", "lat", "lon"}:
        return False
    if primary_core != shadow_core:
        return False

    if not _contains_variable(primary_schema, variable):
        return False
    if not _contains_variable(shadow_schema, variable):
        return False

    return True


def _shadow_targets(config):
    ingest_cfg = config.get("ingest") or {}
    shadow_cfg = ingest_cfg.get("shadow") or {}
    if not shadow_cfg.get("enabled"):
        return set()
    model_source = shadow_cfg.get("model_source") or {}
    targets = set()
    for key in model_source.keys():
        if "-" not in key:
            continue
        group, server_model = key.split("-", 1)
        targets.add((group, server_model))
    return targets


def _parity_record(group, server_model, local_model, variable, primary_match, shadow_match, presence_ok, schema_ok, schema_details):
    return {
        "group": group,
        "server_model": server_model,
        "local_model": local_model,
        "variable": variable,
        "primary_match": primary_match,
        "shadow_match": shadow_match,
        "presence_ok": presence_ok,
        "schema_ok": schema_ok,
        "schema_details": schema_details,
    }


def build_shadow_parity_records(config, fcstdate, lookback_days):
    shadow_cfg = ((config.get("ingest") or {}).get("shadow") or {})
    shadow_root = shadow_cfg.get("rt_root")
    if not (shadow_cfg.get("enabled") and shadow_root):
        return []

    targets = _shadow_targets(config)
    if not targets:
        return []

    primary_records = {
        (r["group"], r["server_model"], r["local_model"], r["variable"]): r
        for r in iter_records(config, fcstdate, lookback_days)
        if (r["group"], r["server_model"]) in targets
    }
    shadow_records = {
        (r["group"], r["server_model"], r["local_model"], r["variable"]): r
        for r in iter_records(config, fcstdate, lookback_days, rt_root=str(shadow_root))
        if (r["group"], r["server_model"]) in targets
    }

    keys = sorted(set(primary_records.keys()) | set(shadow_records.keys()))
    parity_records = []
    for key in keys:
        group, server_model, local_model, variable = key
        primary = primary_records.get(key)
        shadow = shadow_records.get(key)
        primary_match = primary["matches"][0] if primary and primary["matches"] else None
        shadow_match = shadow["matches"][0] if shadow and shadow["matches"] else None
        presence_ok = bool(primary_match and shadow_match)

        schema_ok = None
        schema_details = {}
        if presence_ok:
            primary_schema, primary_error = _schema_snapshot(primary_match)
            shadow_schema, shadow_error = _schema_snapshot(shadow_match)
            schema_details = {
                "primary": primary_schema,
                "shadow": shadow_schema,
            }
            if primary_error or shadow_error:
                schema_details["primary_error"] = primary_error
                schema_details["shadow_error"] = shadow_error
            else:
                schema_details["normalized"] = {
                    "primary_core_dims": _core_dim_sizes(primary_schema),
                    "shadow_core_dims": _core_dim_sizes(shadow_schema),
                    "primary_has_variable": _contains_variable(primary_schema, variable),
                    "shadow_has_variable": _contains_variable(shadow_schema, variable),
                }
                schema_ok = _schemas_compatible(primary_schema, shadow_schema, variable)

        parity_records.append(
            _parity_record(
                group=group,
                server_model=server_model,
                local_model=local_model,
                variable=variable,
                primary_match=primary_match,
                shadow_match=shadow_match,
                presence_ok=presence_ok,
                schema_ok=schema_ok,
                schema_details=schema_details,
            )
        )

    return parity_records


def write_manifest(outdir, fcstdate, records):
    if not outdir:
        return

    output_dir = Path(outdir)
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "fcstdate": fcstdate,
        "checked_at": datetime.utcnow().isoformat() + "Z",
        "records": records,
    }
    with open(output_dir / "validation_manifest.json", "w", encoding="utf-8") as manifest_file:
        json.dump(payload, manifest_file, indent=2)


def write_shadow_parity_manifest(outdir, fcstdate, records):
    if not outdir or not records:
        return

    output_dir = Path(outdir)
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "fcstdate": fcstdate,
        "checked_at": datetime.utcnow().isoformat() + "Z",
        "summary": {
            "total": len(records),
            "presence_ok": sum(1 for r in records if r["presence_ok"]),
            "presence_missing": sum(1 for r in records if not r["presence_ok"]),
            "schema_ok": sum(1 for r in records if r["schema_ok"] is True),
            "schema_mismatch": sum(1 for r in records if r["schema_ok"] is False),
            "schema_unavailable": sum(1 for r in records if r["schema_ok"] is None),
        },
        "records": records,
    }
    with open(output_dir / "shadow_parity_manifest.json", "w", encoding="utf-8") as manifest_file:
        json.dump(payload, manifest_file, indent=2)


def validate_records(records, policy):
    missing = [record for record in records if not record["ok"]]
    for record in records:
        state = "OK" if record["ok"] else "MISSING"
        print(
            "[{}] {}/{} -> {} var={} matches={}".format(
                state,
                record["group"],
                record["server_model"],
                record["local_model"],
                record["variable"],
                len(record["matches"]),
            )
        )

    if missing and policy == "fail_on_missing":
        print("[ERROR] Missing realtime inputs for {} model-variable combinations.".format(len(missing)))
        return 1

    if missing:
        print("[WARN] Missing realtime inputs for {} model-variable combinations.".format(len(missing)))
    else:
        print("[INFO] Realtime input validation passed.")
    return 0


def log_shadow_parity(records):
    if not records:
        return

    total = len(records)
    presence_ok = sum(1 for r in records if r["presence_ok"])
    presence_missing = total - presence_ok
    schema_ok = sum(1 for r in records if r["schema_ok"] is True)
    schema_mismatch = sum(1 for r in records if r["schema_ok"] is False)
    schema_unavailable = sum(1 for r in records if r["schema_ok"] is None)

    print(
        "[PARITY-SUMMARY] total={} presence_ok={} presence_missing={} schema_ok={} schema_mismatch={} schema_unavailable={}".format(
            total,
            presence_ok,
            presence_missing,
            schema_ok,
            schema_mismatch,
            schema_unavailable,
        )
    )

    for record in records:
        state = "OK" if record["presence_ok"] else "MISSING"
        schema_state = "NA"
        if record["schema_ok"] is True:
            schema_state = "OK"
        elif record["schema_ok"] is False:
            schema_state = "MISMATCH"
        print(
            "[PARITY-{}] {}/{} -> {} var={} schema={}".format(
                state,
                record["group"],
                record["server_model"],
                record["local_model"],
                record["variable"],
                schema_state,
            )
        )


def build_parser():
    parser = argparse.ArgumentParser(description="Validate realtime SubX inputs before forecast generation.")
    parser.add_argument("--config", required=True, help="Path to config.yaml")
    parser.add_argument("--fcstdate", help="Forecast date in YYYYMMDD format")
    parser.add_argument("--outdir", help="Optional directory for the validation manifest")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    fcstdate = args.fcstdate or latest_thursday(config.get("fcstdate"))
    lookback_days = int(((config.get("validation") or {}).get("lookback_days") or 7))
    policy = ((config.get("validation") or {}).get("policy") or "fail_on_missing").lower()
    records = list(iter_records(config, fcstdate, lookback_days))
    write_manifest(args.outdir, fcstdate, records)
    shadow_records = build_shadow_parity_records(config, fcstdate, lookback_days)
    write_shadow_parity_manifest(args.outdir, fcstdate, shadow_records)
    log_shadow_parity(shadow_records)
    return validate_records(records, policy)


if __name__ == "__main__":
    raise SystemExit(main())
