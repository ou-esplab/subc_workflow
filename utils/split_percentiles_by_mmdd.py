#!/usr/bin/env python3
"""Split large percentile climatology files into per-MMDD files.

Input pattern (example):
  /data/esplab/gbrown/GWSC/{group}-{model}_{var}95.nc

Output layout:
  /data/esplab/shared/model/initialized/subx/hindcast/{var}{lev}/daily/percentiles/
    {model}-{group}/{var}_{model}-{group}/{var}_{group}-{model}_mmdd.95p.nc
"""

from __future__ import annotations

import argparse
import glob
import os
from pathlib import Path

import xarray as xr


def parse_group_model(input_path: Path, var: str) -> tuple[str, str]:
    stem = input_path.stem
    suffix = f"_{var}95"
    if not stem.endswith(suffix):
        raise ValueError(f"Unexpected filename for var={var}: {input_path.name}")
    prefix = stem[: -len(suffix)]
    if "-" not in prefix:
        raise ValueError(f"Cannot parse group/model from: {input_path.name}")
    group, model = prefix.split("-", 1)
    return group, model


def mmdd_from_coord(value: object) -> str:
    text = str(value)
    # Expected month_day format is 'MM-DD'. Fallback removes separators.
    return text.replace("-", "").replace("/", "")


def split_one_file(src: Path, dest_root: Path, var: str, lev: str, dry_run: bool) -> int:
    group, model = parse_group_model(src, var)

    ds = xr.open_dataset(src)
    if "month_day" not in ds.coords:
        ds.close()
        raise ValueError(f"{src} is missing 'month_day' coordinate")

    month_days = list(ds["month_day"].values)
    out_dir = dest_root / f"{var}{lev}" / "daily" / "percentiles" / f"{model}-{group}" / f"{var}_{model}-{group}"

    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    wrote = 0
    for md in month_days:
        mmdd = mmdd_from_coord(md)
        out_name = f"{var}_{group}-{model}_{mmdd}.95p.nc"
        out_path = out_dir / out_name

        if out_path.exists():
            continue

        if dry_run:
            wrote += 1
            continue

        day_ds = ds.sel(month_day=md)
        if "month_day" not in day_ds.dims:
            day_ds = day_ds.expand_dims(month_day=[md])

        day_ds.to_netcdf(out_path)
        wrote += 1

    ds.close()
    return wrote


def main() -> int:
    p = argparse.ArgumentParser(description="Split threshold files into per-MMDD percentile files")
    p.add_argument("--source-dir", default="/data/esplab/gbrown/GWSC")
    p.add_argument("--dest-root", default="/data/esplab/shared/model/initialized/subx/hindcast")
    p.add_argument("--var", default="pr", help="Variable token used in source suffix and output names")
    p.add_argument("--lev", default="sfc", help="Level token used in destination var+lev directory")
    p.add_argument("--glob", default=None, help="Optional explicit glob relative to source-dir")
    p.add_argument("--dry-run", action="store_true", help="Show what would be written without creating files")
    args = p.parse_args()

    source_dir = Path(args.source_dir)
    dest_root = Path(args.dest_root)

    pattern = args.glob or f"*_{args.var}95.nc"
    files = sorted(Path(p) for p in glob.glob(str(source_dir / pattern)))

    if not files:
        print(f"No files matched: {source_dir / pattern}")
        return 2

    total = 0
    for src in files:
        try:
            wrote = split_one_file(src, dest_root, args.var, args.lev, args.dry_run)
            total += wrote
            mode = "would write" if args.dry_run else "wrote"
            print(f"{src.name}: {mode} {wrote} files")
        except Exception as exc:
            print(f"[ERROR] {src.name}: {exc}")

    print(f"Total {'candidate' if args.dry_run else 'new'} files: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
