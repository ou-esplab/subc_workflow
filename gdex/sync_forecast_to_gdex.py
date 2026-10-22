"""
Recurring pipeline stage: sync new SubX forecast raw model files to the
NCAR GDEX public mirror over Globus.

Raw model files only (paths.rt_root / gdex.input_root), mirroring the
current local layout under gdex.dest_root:
    {GROUP}-{MODEL}/forecast/{VAR}/{VAR}_{GROUP}-{MODEL}_{YYYYMMDD}.daily.nc

State is inferred purely from what's already present at the destination
(via `globus ls`) -- no local manifest/state file -- the same "trust the
destination" approach ArrayLake's sync uses.
"""
import argparse
import logging
import os
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


def build_runtime_config() -> dict:
    parser = argparse.ArgumentParser(description="Sync SubX forecast raw files to NCAR GDEX")
    parser.add_argument("--config", required=True, help="Path to workflow config.yaml")
    parser.add_argument("--date", default=None, help="Optional target init date YYYYMMDD")
    parser.add_argument("--dry-run", action="store_true", help="Scan and report pending transfers without submitting them")
    args = parser.parse_args()

    runtime = gu.load_gdex_config(args.config)

    date_arg = args.date.strip() if isinstance(args.date, str) and args.date.strip() else None
    if date_arg is not None:
        try:
            datetime.strptime(date_arg, "%Y%m%d")
        except ValueError as e:
            raise RuntimeError(f"Invalid --date value {date_arg}; expected YYYYMMDD") from e
    runtime["date_filter"] = date_arg
    runtime["dry_run"] = bool(args.dry_run or os.environ.get("GDEX_DRY_RUN") == "1")
    return runtime


def main() -> int:
    runtime = build_runtime_config()
    input_root = str(runtime["input_root"]).rstrip("/")
    dest_root = runtime["dest_root"]
    date_filter = runtime["date_filter"]
    dry_run = runtime["dry_run"]

    logger.info("=" * 70)
    logger.info(f"Starting GDEX forecast sync at {datetime.now()}")
    logger.info("=" * 70)

    model_results = []
    jobs = {}  # label -> pairs to transfer, submitted together after discovery
    for label, effective_vars in gu.resolve_models(runtime):
        try:
            pairs = []
            per_var_missing = {}
            for var in effective_vars:
                local_dir = Path(input_root) / label / "forecast" / var
                if not local_dir.is_dir():
                    continue
                glob_pattern = f"{var}_{label}_{date_filter if date_filter else '????????'}.daily.nc"
                local_files = sorted(local_dir.glob(glob_pattern))
                if not local_files:
                    continue

                dest_var_dir = f"{label}/forecast/{var}"
                remote_names = set(gu.globus_ls(runtime["dest_endpoint_id"], f"{dest_root}/{dest_var_dir}"))
                missing = [f for f in local_files if f.name not in remote_names]
                if missing:
                    per_var_missing[var] = len(missing)
                for f in missing:
                    pairs.append((f"{label}/forecast/{var}/{f.name}", f"{dest_var_dir}/{f.name}"))

            if not pairs:
                logger.info(f"{label}: nothing to sync (already up to date on GDEX)")
                model_results.append((label, True, "Nothing to sync"))
                continue

            summary = ", ".join(f"{var}:{count}" for var, count in per_var_missing.items())
            logger.info(f"{label}: {len(pairs)} file(s) missing on GDEX ({summary})")

            if dry_run:
                for src_rel, dst_rel in pairs:
                    logger.info(f"  [DRY-RUN] would transfer {src_rel} -> {dst_rel}")
                model_results.append((label, True, f"Dry-run: {len(pairs)} file(s) would be transferred"))
                continue

            jobs[label] = pairs

        except Exception as e:
            logger.error(f"✗ Unexpected error processing {label}: {e}")
            logger.error(traceback.format_exc())
            model_results.append((label, False, f"Unexpected error: {e}"))

    if jobs:
        gu.ensure_source_endpoint_online(runtime, logger)
        logger.info(f"Submitting {sum(len(p) for p in jobs.values())} file(s) across {len(jobs)} model(s) "
                    f"in chunks of {runtime['batch_chunk_size']}, up to {runtime['max_active_tasks']} tasks at once")
        for label, (ok, detail) in gu.transfer_batches_parallel(
                runtime, jobs, "subx-gdex-forecast", logger).items():
            if ok:
                logger.info(f"✓ {label}: {detail}")
            else:
                logger.error(f"✗ {label}: {detail}")
            model_results.append((label, ok, detail))

    logger.info("")
    logger.info("=" * 70)
    logger.info("GDEX FORECAST SYNC SUMMARY")
    logger.info("=" * 70)
    successful = sum(1 for _, success, _ in model_results if success)
    failed = len(model_results) - successful
    logger.info(f"Total models processed: {len(model_results)}")
    logger.info(f"Successful: {successful}")
    logger.info(f"Failed: {failed}")
    logger.info("")
    for label, success, message in model_results:
        status = "✓" if success else "✗"
        logger.info(f"{status} {label}: {message}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
