"""
One-off helper: re-send local archive files that were modified in place to the NCAR GDEX mirror.

The recurring sync (sync_forecast_to_gdex.py) only sends files that are *missing by name* at the
destination, so it never refreshes a file that was edited locally afterwards (for example the
L-coordinate relabels from utils/relabel_archive_leads.py and
utils/rewrite_geos_v3_hindcast_leads.py). This reads their JSONL manifests (one record per
changed file, with a "path" key) and submits the paths as chunked Globus batch tasks. Transfers
use --sync-level checksum, so a destination file that differs is overwritten and an identical
one is skipped.

Usage:
  python3 gdex/resend_changed_to_gdex.py --config config.yaml \
      --manifest logs/lead_relabel_manifest.jsonl logs/geos_v3_lead_rewrite_manifest.jsonl --dry-run
  python3 gdex/resend_changed_to_gdex.py --config config.yaml --manifest ... [--models GMAO-GEOS_V3]
"""
import argparse
import json
import logging
import sys
import traceback
from datetime import datetime
from pathlib import Path

import gdex_utils as gu

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


def collect_jobs(manifests, input_root, allowed):
    """Return ({label: [(rel, rel), ...]}, skipped_counts) from manifest records.

    ``allowed`` maps model label -> effective variables mirrored to GDEX; anything else
    (other roots, unmirrored variables, temp files) is skipped and counted.
    """
    root = Path(input_root)
    seen = set()
    jobs = {}
    skipped = {"outside_root": 0, "not_mirrored": 0, "missing_locally": 0, "tmp": 0}
    for manifest in manifests:
        with open(manifest, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                path = Path(json.loads(line)["path"])
                if path in seen:
                    continue
                seen.add(path)
                if path.name.endswith(".tmp.nc"):
                    skipped["tmp"] += 1
                    continue
                try:
                    rel = path.relative_to(root)
                except ValueError:
                    skipped["outside_root"] += 1
                    continue
                # <label>/<forecast|hindcast>/<var>/<file>
                if len(rel.parts) != 4 or rel.parts[0] not in allowed or rel.parts[2] not in allowed[rel.parts[0]]:
                    skipped["not_mirrored"] += 1
                    continue
                if not path.is_file():
                    skipped["missing_locally"] += 1
                    continue
                jobs.setdefault(rel.parts[0], []).append((str(rel), str(rel)))
    return jobs, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True, help="Path to workflow config.yaml")
    parser.add_argument("--manifest", nargs="+", required=True, help="JSONL manifest(s) of modified files")
    parser.add_argument("--models", nargs="+", default=None, help="Restrict to these {GROUP}-{MODEL} labels")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be re-sent without submitting")
    args = parser.parse_args()

    runtime = gu.load_gdex_config(args.config)
    input_root = str(runtime["input_root"]).rstrip("/")
    allowed = {label: set(vars_) for label, vars_ in gu.resolve_models(runtime)
               if not args.models or label in args.models}

    logger.info("=" * 70)
    logger.info(f"Starting GDEX re-send of locally modified files at {datetime.now()}")
    logger.info("=" * 70)

    jobs, skipped = collect_jobs(args.manifest, input_root, allowed)
    for label, pairs in sorted(jobs.items()):
        logger.info(f"{label}: {len(pairs)} file(s) to re-send")
    logger.info(f"Skipped: {skipped}")
    if not jobs:
        logger.info("Nothing to re-send")
        return 0
    if args.dry_run:
        for label, pairs in sorted(jobs.items()):
            for src_rel, _ in pairs[:3]:
                logger.info(f"  [DRY-RUN] e.g. {src_rel}")
        logger.info("Dry-run complete; no tasks submitted")
        return 0

    failed = 0
    try:
        gu.ensure_source_endpoint_online(runtime, logger)
        for label, (ok, detail) in gu.transfer_batches_parallel(runtime, jobs, "subx-gdex-resend", logger).items():
            logger.info(f"{'✓' if ok else '✗'} {label}: {detail}")
            failed += 0 if ok else 1
    except Exception as e:
        logger.error(f"✗ re-send failed: {e}")
        logger.error(traceback.format_exc())
        return 1
    logger.info(f"Models successful: {len(jobs) - failed}, failed: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
