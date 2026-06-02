#!/usr/bin/env python3
"""Split pre-computed GWSC percentile files into per-MMDD files for all models.

Source files (from G. Brown's GWSC directory):
  {gwsc-dir}/{group}-{model}_{var}{pct}.nc
  or
  {gwsc-dir}/percentile{pct}_{modelkey}thres_{var}.nc

Writes per-MMDD files to:
  {hc_root}/{var}{lev}/daily/percentiles/{model}-{group}/{var}_{model}-{group}/
    {var}_{group}-{model}_{MMDD}.{pct}p.nc

This is a generalized version of utils/split_percentiles_by_mmdd.py that
supports configurable percentile values and reads paths from config.yaml.

Examples:
  # Split all pr 95th-percentile files
  python split_subx_percentiles.py --config ../config.yaml --var pr --lev sfc

  # Split with explicit GWSC directory
  python split_subx_percentiles.py --config ../config.yaml --var pr --lev sfc \\
      --gwsc-dir /data/esplab/gbrown/GWSC --percentile 95

  # Dry run to preview
  python split_subx_percentiles.py --config ../config.yaml --var pr --lev sfc --dry-run
"""

from __future__ import annotations

import argparse
import glob
from pathlib import Path

import xarray as xr
import yaml


def _load_paths(config_path: str) -> tuple[str, str]:
    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    hc_root = (cfg.get("paths") or {}).get("hc_root", "")
    if not hc_root:
        raise RuntimeError("paths.hc_root not set in config.yaml")
    gwsc = (cfg.get("paths") or {}).get("gwsc_root", "/data/esplab/gbrown/GWSC")
    return hc_root, gwsc


def _parse_group_model(src: Path, var: str, pct: int) -> tuple[str, str]:
    """Extract group and model from source filename.

    Handles patterns like:
      EMC-GEFSv12_CPC_pr95.nc  → group=EMC, model=GEFSv12_CPC
      GEFSthres_pr.nc           → falls back to explicit --group/--model
    """
    stem = src.stem
    suffix = f"_{var}{pct}"
    if stem.endswith(suffix):
        prefix = stem[: -len(suffix)]
        if "-" in prefix:
            group, model = prefix.split("-", 1)
            return group, model
    raise ValueError(f"Cannot parse group/model from: {src.name}  (expected pattern: {{GROUP}}-{{MODEL}}_{var}{pct}.nc)")


def _mmdd_from_coord(value: object) -> str:
    return str(value).replace("-", "").replace("/", "")


def split_one_file(
    src: Path,
    hc_root: Path,
    var: str,
    lev: str,
    pct: int,
    group: str,
    model: str,
    dry_run: bool,
    overwrite: bool,
) -> int:
    ds = xr.open_dataset(src)
    if "month_day" not in ds.coords:
        ds.close()
        raise ValueError(f"{src} is missing 'month_day' coordinate")

    month_days = list(ds["month_day"].values)
    out_dir = (
        hc_root
        / f"{var}{lev}"
        / "daily"
        / "percentiles"
        / f"{model}-{group}"
        / f"{var}_{model}-{group}"
    )

    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    wrote = 0
    for md in month_days:
        mmdd = _mmdd_from_coord(md)
        out_name = f"{var}_{group}-{model}_{mmdd}.{pct}p.nc"
        out_path = out_dir / out_name

        if out_path.exists() and not overwrite:
            continue

        if dry_run:
            wrote += 1
            continue

        day_ds = ds.sel(month_day=md)
        if "month_day" not in day_ds.dims:
            day_ds = day_ds.expand_dims(month_day=[md])

        tmp = out_path.with_suffix(".tmp.nc")
        day_ds.to_netcdf(tmp)
        tmp.rename(out_path)
        wrote += 1

    ds.close()
    return wrote


def main() -> int:
    p = argparse.ArgumentParser(description="Split GWSC percentile files into per-MMDD files")
    p.add_argument("--config",      default="../config.yaml", help="Path to config.yaml")
    p.add_argument("--gwsc-dir",    default=None,             help="Override GWSC source directory")
    p.add_argument("--var",         required=True,            help="Variable (e.g. pr, tas)")
    p.add_argument("--lev",         required=True,            help="Level suffix (e.g. sfc, 2m, 500)")
    p.add_argument("--percentile",  type=int, default=95,     help="Percentile value (default: 95)")
    p.add_argument("--glob",        default=None,             help="Optional explicit glob pattern relative to gwsc-dir")
    p.add_argument("--dry-run",     action="store_true",      help="Preview without writing files")
    p.add_argument("--overwrite",   action="store_true",      help="Overwrite existing per-MMDD files")
    args = p.parse_args()

    hc_root_str, gwsc_default = _load_paths(args.config)
    hc_root = Path(hc_root_str)
    gwsc_dir = Path(args.gwsc_dir) if args.gwsc_dir else Path(gwsc_default)
    pct = args.percentile

    pattern = args.glob or f"*-*_{args.var}{pct}.nc"
    files = sorted(Path(f) for f in glob.glob(str(gwsc_dir / pattern)))

    if not files:
        print(f"No files matched: {gwsc_dir / pattern}")
        return 2

    total = 0
    for src in files:
        try:
            group, model = _parse_group_model(src, args.var, pct)
            wrote = split_one_file(
                src=src,
                hc_root=hc_root,
                var=args.var,
                lev=args.lev,
                pct=pct,
                group=group,
                model=model,
                dry_run=args.dry_run,
                overwrite=args.overwrite,
            )
            mode = "would write" if args.dry_run else "wrote"
            print(f"{src.name} ({group}-{model}): {mode} {wrote} files")
            total += wrote
        except Exception as exc:
            print(f"[ERROR] {src.name}: {exc}")

    print(f"\nTotal {'candidate' if args.dry_run else 'new'} files: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
