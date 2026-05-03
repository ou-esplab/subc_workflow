#!/usr/bin/env python3
"""Preflight validation for realtime SubX inputs.

The forecast stage depends on daily realtime files already being present on disk.
This validator checks that each configured model-variable combination has at least
one matching file for the requested forecast date and writes a JSON manifest that
documents what was found.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

import yaml


@dataclass
class ValidationRecord:
    group: str
    server_model: str
    local_model: str
    variable: str
    pattern: str
    matches: list[str]

    @property
    def ok(self) -> bool:
        return bool(self.matches)


def latest_thursday(date_str: str | None = None) -> str:
    if date_str:
        current = datetime.strptime(date_str, "%Y%m%d")
    else:
        current = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return (current - timedelta(days=(current.weekday() - 3) % 7)).strftime("%Y%m%d")


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as config_file:
        return yaml.safe_load(config_file) or {}


def resolve_local_model(group: str, server_model: str, mapping: dict[str, str]) -> str:
    return mapping.get(f"{group}-{server_model}", mapping.get(server_model, server_model))


def iter_records(config: dict, fcstdate: str) -> Iterable[ValidationRecord]:
    rt_root = (config.get("paths") or {}).get("rt_root")
    if not rt_root:
        raise ValueError("config.paths.rt_root is required")

    model_map = config.get("model_name_map") or {}
    models = config.get("models") or []
    if not models:
        raise ValueError("config.models must contain at least one model entry")

    for model in models:
        group = model["group"]
        server_model = model["name"]
        local_model = resolve_local_model(group, server_model, model_map)
        for variable in model.get("vars") or []:
            pattern = os.path.join(
                rt_root,
                f"{group}-{local_model}",
                "forecast",
                variable,
                f"{variable}_{group}-{local_model}_{fcstdate}*.daily.nc",
            )
            yield ValidationRecord(
                group=group,
                server_model=server_model,
                local_model=local_model,
                variable=variable,
                pattern=pattern,
                matches=sorted(glob.glob(pattern)),
            )


def write_manifest(outdir: str | None, fcstdate: str, records: list[ValidationRecord]) -> None:
    if not outdir:
        return

    output_dir = Path(outdir)
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "fcstdate": fcstdate,
        "checked_at": datetime.utcnow().isoformat() + "Z",
        "records": [asdict(record) | {"ok": record.ok} for record in records],
    }
    with open(output_dir / "validation_manifest.json", "w", encoding="utf-8") as manifest_file:
        json.dump(payload, manifest_file, indent=2)


def validate_records(records: list[ValidationRecord], policy: str) -> int:
    missing = [record for record in records if not record.ok]
    for record in records:
        state = "OK" if record.ok else "MISSING"
        print(
            f"[{state}] {record.group}/{record.server_model} -> {record.local_model} "
            f"var={record.variable} matches={len(record.matches)}"
        )

    if missing and policy == "fail_on_missing":
        print(f"[ERROR] Missing realtime inputs for {len(missing)} model-variable combinations.")
        return 1

    if missing:
        print(f"[WARN] Missing realtime inputs for {len(missing)} model-variable combinations.")
    else:
        print("[INFO] Realtime input validation passed.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate realtime SubX inputs before forecast generation.")
    parser.add_argument("--config", required=True, help="Path to config.yaml")
    parser.add_argument("--fcstdate", help="Forecast date in YYYYMMDD format")
    parser.add_argument("--outdir", help="Optional directory for the validation manifest")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    fcstdate = args.fcstdate or latest_thursday(config.get("fcstdate"))
    policy = ((config.get("validation") or {}).get("policy") or "fail_on_missing").lower()
    records = list(iter_records(config, fcstdate))
    write_manifest(args.outdir, fcstdate, records)
    return validate_records(records, policy)


if __name__ == "__main__":
    raise SystemExit(main())
