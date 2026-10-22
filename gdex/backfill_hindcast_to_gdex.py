"""
One-time standalone script: bulk-copy each model's whole local hindcast
archive to the NCAR GDEX public mirror over Globus.

Not wired into runners/cli.py or scripts/cron_subx.sh -- run manually,
inside a screen session, once per archive (or per --models subset for
testing). By default uses one recursive `globus transfer --recursive`
per model, since the hindcast archive is large and mostly static; pass
--limit N for a small batch-transfer test of just N files per model
instead (every configured model's hindcast archive runs into the
hundreds of GB, so "small" only comes from --limit, not from picking a
smaller model).

Usage:
  python3 gdex/backfill_hindcast_to_gdex.py --config config.yaml --models GMAO-GEOS_V3 --limit 5 --dry-run
  python3 gdex/backfill_hindcast_to_gdex.py --config config.yaml --models GMAO-GEOS_V3 --limit 5
  python3 gdex/backfill_hindcast_to_gdex.py --config config.yaml --dry-run
  python3 gdex/backfill_hindcast_to_gdex.py --config config.yaml
"""
import argparse
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


def _discover_limited_files(input_root: str, label: str, effective_vars: list[str], limit: int):
    """Return up to `limit` (src_rel, dst_rel) hindcast file pairs for one model, in variable order."""
    pairs = []
    for var in effective_vars:
        if len(pairs) >= limit:
            break
        local_dir = Path(input_root) / label / "hindcast" / var
        if not local_dir.is_dir():
            continue
        for f in sorted(local_dir.glob(f"{var}_{label}_????????.daily.nc")):
            if len(pairs) >= limit:
                break
            rel = f"{label}/hindcast/{var}/{f.name}"
            pairs.append((rel, rel))
    return pairs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True, help="Path to workflow config.yaml")
    parser.add_argument("--models", nargs="+", default=None,
                         help="Restrict backfill to these {GROUP}-{MODEL} labels (default: all configured models)")
    parser.add_argument("--limit", type=int, default=None,
                         help="Test mode: batch-transfer only the first N hindcast files per selected model "
                              "(across its configured variables), instead of a full recursive directory transfer")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be transferred without submitting tasks")
    parser.add_argument("--attach", nargs="+", default=[], metavar="MODEL=TASK_ID",
                         help="Do not submit; wait on an already-running Globus task for MODEL "
                              "(e.g. after a previous run of this script exited while its transfer kept running)")
    parser.add_argument("--max-retries", type=int, default=2, help="Retries per model on task failure/timeout")
    args = parser.parse_args()

    runtime = gu.load_gdex_config(args.config)
    input_root = str(runtime["input_root"]).rstrip("/")
    dest_root = runtime["dest_root"]

    models_and_vars = [(label, effective_vars) for label, effective_vars in gu.resolve_models(runtime)
                        if not args.models or label in args.models]
    if not models_and_vars:
        logger.error("No models selected for backfill (check --models / gdex.skip_models)")
        return 1
    labels = [label for label, _ in models_and_vars]

    logger.info("=" * 70)
    logger.info(f"Starting GDEX hindcast backfill at {datetime.now()}")
    logger.info(f"Models ({len(labels)}): {', '.join(labels)}")
    if args.limit is not None:
        logger.info(f"Limit mode: up to {args.limit} file(s) per model, batch transfer (not recursive)")
    logger.info("=" * 70)

    if not args.dry_run:
        gu.ensure_source_endpoint_online(runtime, logger)

    # Submit all per-model transfers up front so they run concurrently.
    tasks = {}
    attach = dict(a.split("=", 1) for a in args.attach)
    for lbl, tid in attach.items():
        if lbl not in labels:
            logger.error(f"--attach {lbl}: not a selected model")
            return 1
        tasks[lbl] = tid
        logger.info(f"{lbl}: attaching to existing task {tid}")
    skipped_labels = set()  # no local data: not a failure
    pairs_by_label = {}  # only populated in --limit mode, needed to resubmit on retry
    for label, effective_vars in models_and_vars:
        if label in attach:
            continue
        if args.limit is not None:
            pairs = _discover_limited_files(input_root, label, effective_vars, args.limit)
            if not pairs:
                logger.warning(f"{label}: no local hindcast files found for configured variables, skipping")
                skipped_labels.add(label)
                continue
            if args.dry_run:
                logger.info(f"[DRY-RUN] would batch-transfer {len(pairs)} file(s) for {label}:")
                for src_rel, dst_rel in pairs:
                    logger.info(f"  {src_rel} -> {dst_rel}")
                continue
            try:
                task_id = gu.globus_transfer_batch(
                    runtime["source_endpoint_id"], runtime["dest_endpoint_id"],
                    input_root, dest_root, pairs,
                    label=f"subx-gdex-hindcast-backfill-test-{label}",
                )
                logger.info(f"{label}: submitted test batch task {task_id} ({len(pairs)} file(s))")
                tasks[label] = task_id
                pairs_by_label[label] = pairs
            except Exception as e:
                logger.error(f"✗ {label}: failed to submit transfer: {e}")
                logger.error(traceback.format_exc())
        else:
            src_path = f"{input_root}/{label}/hindcast"
            if not Path(src_path).is_dir():
                logger.warning(f"{label}: no local hindcast dir at {src_path}, skipping")
                skipped_labels.add(label)
                continue
            dst_path = f"{dest_root}/{label}/hindcast"
            if args.dry_run:
                logger.info(f"[DRY-RUN] would submit recursive transfer {src_path} -> {dst_path}")
                continue
            try:
                task_id = gu.globus_transfer_recursive(
                    runtime["source_endpoint_id"], runtime["dest_endpoint_id"],
                    src_path, dst_path, label=f"subx-gdex-hindcast-backfill-{label}",
                )
                logger.info(f"{label}: submitted recursive backfill task {task_id}")
                tasks[label] = task_id
            except Exception as e:
                existing = gu.globus_find_unfinished_task(f"subx-gdex-hindcast-backfill-{label}") \
                    if "identical paths" in str(e) else None
                if existing:
                    logger.info(f"{label}: identical transfer already in flight; attaching to task {existing}")
                    tasks[label] = existing
                else:
                    logger.error(f"✗ {label}: failed to submit transfer: {e}")
                    logger.error(traceback.format_exc())

    if args.dry_run:
        logger.info("Dry-run complete; no tasks submitted")
        return 0

    results = [(label, False, "Failed to submit transfer") for label in labels
               if label not in tasks and label not in skipped_labels]
    for label, task_id in tasks.items():
        ok, detail = False, ""
        attempt = 0
        while not ok and attempt <= args.max_retries:
            attempt += 1
            logger.info(f"{label}: waiting on task {task_id} (attempt {attempt}/{args.max_retries + 1})")
            try:
                ok, detail = gu.globus_task_wait(
                    task_id, runtime["task_poll_interval_s"], runtime["task_wait_timeout_s"]
                )
            except Exception as e:
                ok, detail = False, f"Unexpected error waiting on task: {e}"
                logger.error(traceback.format_exc())
            if not ok:
                status = gu.globus_task_status(task_id)
                if status == "SUCCEEDED":  # finished between the wait timeout and this check
                    ok, detail = True, "SUCCEEDED"
                    break
                if status in ("ACTIVE", "INACTIVE"):
                    # Wait timed out but the task is still alive server-side: keep waiting on
                    # it. Resubmitting would only be rejected (409) and abandon a healthy transfer.
                    logger.info(f"{label}: task {task_id} still {status} after wait timeout; continuing to wait")
                    attempt -= 1
                    continue
            if not ok and attempt <= args.max_retries:
                logger.warning(f"{label}: task {task_id} did not succeed ({detail}); resubmitting")
                try:
                    if label in pairs_by_label:
                        task_id = gu.globus_transfer_batch(
                            runtime["source_endpoint_id"], runtime["dest_endpoint_id"],
                            input_root, dest_root, pairs_by_label[label],
                            label=f"subx-gdex-hindcast-backfill-test-{label}-retry{attempt}",
                        )
                    else:
                        src_path = f"{input_root}/{label}/hindcast"
                        dst_path = f"{dest_root}/{label}/hindcast"
                        task_id = gu.globus_transfer_recursive(
                            runtime["source_endpoint_id"], runtime["dest_endpoint_id"],
                            src_path, dst_path, label=f"subx-gdex-hindcast-backfill-{label}-retry{attempt}",
                        )
                except Exception as e:
                    detail = f"Failed to resubmit: {e}"
                    logger.error(traceback.format_exc())
                    break
        results.append((label, ok, detail if not ok else f"Completed (task {task_id})"))

    logger.info("")
    logger.info("=" * 70)
    logger.info("HINDCAST BACKFILL SUMMARY")
    logger.info("=" * 70)
    successful = sum(1 for _, ok, _ in results if ok)
    failed = len(results) - successful
    logger.info(f"Total models processed: {len(results)}")
    logger.info(f"Successful: {successful}")
    logger.info(f"Failed: {failed}")
    logger.info("")
    for label, ok, detail in results:
        status = "✓" if ok else "✗"
        logger.info(f"{status} {label}: {detail}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
