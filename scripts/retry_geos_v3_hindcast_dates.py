#!/usr/bin/env python3
"""One-off targeted retry for specific GEOS_V3 hindcast init dates.

Reuses static/download_geos_v3_hindcast.py's per-date download function
directly (rather than looping the CLI script once per date) so the
1874-date server listing only happens once instead of once per retried
date. Always passes overwrite=True since every target date here already
has an existing (partial or missing) output file that a normal
skip-if-exists run wouldn't touch.

Usage:
  python3 scripts/retry_geos_v3_hindcast_dates.py --config config.yaml
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from static.download_geos_v3_hindcast import (  # noqa: E402
    BASE_URL,
    _download_one_init_date,
)

# 2 partial-member dates from the 2026-07-17 backfill (today), 102
# pre-existing partial-member dates (already in the archive before that
# backfill), and 3 dates that failed entirely (server listed the date dir
# but had no ensemble member files) -- see
# project_geos_v3_hindcast_extended_backfill memory for how this list was
# derived.
TARGET_DATES = sorted(set("""
20150411 20150416
19991018 19991107 20030307 20041202 20041222 20041227 20050106 20050111
20050126 20050210 20050215 20050302 20050317 20050322 20050401 20050406
20050416 20050421 20050501 20050511 20050521 20050526 20050605 20050610
20050615 20050630 20050710 20050720 20050725 20050804 20050819 20050903
20050908 20050913 20050918 20050923 20051003 20051018 20051023 20051028
20051102 20051107 20051112 20051122 20051127 20051202 20051222 20051227
20060111 20060131 20060215 20060220 20060225 20060302 20060312 20060317
20060322 20060327 20060401 20060406 20060421 20060426 20101112 20101212
20110101 20110111 20110126 20110131 20110205 20110210 20110215 20110220
20110302 20110307 20110312 20110317 20110327 20110411 20110426 20110501
20110506 20110511 20110516 20110521 20110630 20110705 20110710 20110715
20110720 20110725 20110730 20110809 20110814 20121107 20180426 20210131
20210506 20210516 20210521 20240406 20240416 20240426
20210511 20240411 20240421
""".split()))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", required=True)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--sleep-dates", type=float, default=3.0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    rt_root = (cfg.get("paths") or {}).get("rt_root", "")
    if not rt_root:
        print("[ERROR] paths.rt_root not set in config.yaml", file=sys.stderr)
        return 1

    print(f"Retrying {len(TARGET_DATES)} target dates (overwrite=True)\n")

    total = {"wrote": 0, "skipped": 0, "failed": 0}
    for i, init_date in enumerate(TARGET_DATES, start=1):
        date_url = urljoin(BASE_URL, f"{init_date}/")
        print(f"\n=== [{i}/{len(TARGET_DATES)}] {init_date} ===")
        try:
            counts = _download_one_init_date(
                date_url=date_url,
                init_date=init_date,
                rt_root=rt_root,
                overwrite=True,
                dry_run=args.dry_run,
                workers=args.workers,
            )
        except Exception as exc:
            print(f"  [ERROR] {init_date} failed: {exc}")
            counts = {"wrote": 0, "skipped": 0, "failed": 1}
        for k in total:
            total[k] += counts[k]
        if i < len(TARGET_DATES) and not args.dry_run:
            time.sleep(args.sleep_dates)

    print("\n=== Retry complete ===")
    print(f"  wrote={total['wrote']}  skipped={total['skipped']}  failed={total['failed']}")
    return 0 if total["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
