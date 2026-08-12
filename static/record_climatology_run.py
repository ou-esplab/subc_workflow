#!/usr/bin/env python3
"""Append a record of one climatology/percentile generation run to a manifest.

run_all_static.sh calls this once after each stage (climatology, percentile)
completes, so that the reference period, percentile values, entries covered,
git commit, and timestamp used to produce the current climo/percentile files
are always recoverable later -- without this, there is no way to know what
period an existing file was built on other than file mtimes, which get reset
by unrelated operations (e.g. a full archive re-download) and so cannot be
trusted as a historical record (confirmed the hard way for GEOS_V2p1_5daily's
original climatology, whose reference period could not be reconstructed).

The manifest is append-only JSONL (one JSON object per line) at
{hc_root}/CLIMATOLOGY_MANIFEST.jsonl, so concurrent/sequential runs never need
a read-modify-write cycle.

Usage:
  python3 record_climatology_run.py --hc-root <path> --stage climo \
      --start-year 2001 --end-year 2016 --entries "ECCC-GEPS8 pr sfc,..."

  python3 record_climatology_run.py --hc-root <path> --stage percentile \
      --start-year 2001 --end-year 2016 --percentiles 5,33,67,95 --window 15 \
      --entries "ECCC-GEPS8 pr sfc,..."
"""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            text=True,
        ).strip()
    except Exception:
        return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hc-root", required=True, help="hc_root path (manifest is written here)")
    ap.add_argument("--stage", required=True, choices=["climo", "percentile"])
    ap.add_argument("--start-year", type=int, required=True)
    ap.add_argument("--end-year", type=int, required=True)
    ap.add_argument("--percentiles", default=None,
                     help="Comma-separated percentile values (percentile stage only)")
    ap.add_argument("--window", type=int, default=None,
                     help="Pooling window in days (percentile stage only)")
    ap.add_argument("--entries", required=True,
                     help="Comma-separated 'GROUP-MODEL var lev' triples actually processed")
    ap.add_argument("--failed-entries", default="",
                     help="Comma-separated 'GROUP-MODEL var lev[.pct]' entries that failed")
    args = ap.parse_args()

    entries = [e.strip() for e in args.entries.split(",") if e.strip()]
    failed = [e.strip() for e in args.failed_entries.split(",") if e.strip()]

    record = {
        "stage": args.stage,
        "start_year": args.start_year,
        "end_year": args.end_year,
        "percentiles": (
            [int(p) for p in args.percentiles.split(",")] if args.percentiles else None
        ),
        "window": args.window,
        "n_entries": len(entries),
        "entries": entries,
        "n_failed": len(failed),
        "failed_entries": failed,
        "git_commit": _git_commit(),
        "hostname": socket.gethostname(),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }

    manifest_path = Path(args.hc_root) / "CLIMATOLOGY_MANIFEST.jsonl"
    with open(manifest_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")

    print(f"[record_climatology_run] Appended {args.stage} record "
          f"({len(entries)} entries, {len(failed)} failed) to {manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
