#!/usr/bin/env bash
# cron_subx.sh — weekly SubX forecast workflow cron wrapper
#
# Every run logs its own full output automatically to
# logs/cron/subx_<timestamp>.log (see the `exec > >(tee ...)` line below), so
# crontab entries don't need to redirect to a log file for that -- redirect to
# /dev/null just to stop cron from emailing/dropping the handful of lines
# printed before that tee is set up (e.g. a failure sourcing conda.sh).
#
# Intended crontab entries:
#   # Full weekly pipeline, every Thursday at 18:00 UTC:
#   0 18 * * 4  /home/kpegion/projects/subc_workflow/scripts/cron_subx.sh >/dev/null 2>&1
#
#   # Daily ingest-only top-up, so the Thursday run has less left to fetch:
#   0 6 * * *   SUBX_STAGES=ingest /home/kpegion/projects/subc_workflow/scripts/cron_subx.sh >/dev/null 2>&1
#
# Optional overrides via environment variables:
#   SUBX_CONFIG   — path to config YAML (default: config.yaml next to this script)
#   SUBX_INIT     — override forecast init date as YYYYMMDD (default: latest Thursday)
#   SUBX_STAGES   — space-separated stage list (default: "ingest preprocess products publish arraylake")
#                   (pycpt disabled by default; see below)
#   CONDA_BASE    — path to miniconda/anaconda root (default: ~/miniconda3)
#   ENV_NAME      — conda environment name (default: subc_workflow_env)

set -euo pipefail

# ---- Resolve script location ------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

# ---- Configuration ----------------------------------------------------------
CONFIG="${SUBX_CONFIG:-$ROOT_DIR/config.yaml}"
STAGES="${SUBX_STAGES:-ingest preprocess products publish arraylake}"
CONDA_BASE="${CONDA_BASE:-$HOME/miniconda3}"
ENV_NAME="${ENV_NAME:-subc_workflow_env}"
export SUBC_PYCPT_ENV="${SUBC_PYCPT_ENV:-subc_pycpt_2_8_2}"
LOCK_FILE="/tmp/subx_cron.lock"

# ---- Logging ----------------------------------------------------------------
TS="$(date -u +%Y%m%d_%H%M%S)"
LOG_DIR="$ROOT_DIR/logs/cron"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/subx_${TS}.log"

log() { echo "[$(date -u +%F\ %T\ UTC)] $*"; }

exec > >(tee -a "$LOG_FILE") 2>&1

log "==> SubX cron run starting (TS=$TS)"

# ---- Lock: prevent overlapping runs -----------------------------------------
if [ -e "$LOCK_FILE" ]; then
    LOCKED_PID="$(cat "$LOCK_FILE" 2>/dev/null || echo unknown)"
    if kill -0 "$LOCKED_PID" 2>/dev/null; then
        log "[WARN] Another run is active (PID=$LOCKED_PID). Exiting."
        exit 0
    else
        log "[INFO] Stale lock found (PID=$LOCKED_PID); removing."
        rm -f "$LOCK_FILE"
    fi
fi
echo $$ > "$LOCK_FILE"
trap 'rm -f "$LOCK_FILE"; log "==> Lock released."' EXIT

# ---- Conda setup ------------------------------------------------------------
CONDA_SH="$CONDA_BASE/etc/profile.d/conda.sh"
if [[ ! -f "$CONDA_SH" ]]; then
    log "[FATAL] conda.sh not found at $CONDA_SH. Set CONDA_BASE."
    exit 2
fi
# Disable set -u briefly: conda.sh may reference unset variables (e.g. PS1)
# in non-interactive shells, which would otherwise abort the script.
set +u
# shellcheck source=/dev/null
source "$CONDA_SH"
set -u
log "[INFO] Using conda env: $ENV_NAME"

# ---- Resolve forecast date --------------------------------------------------
if [[ -n "${SUBX_INIT:-}" ]]; then
    INIT_DATE="$SUBX_INIT"
    log "[INFO] Using provided init date: $INIT_DATE"
else
    INIT_DATE="$(conda run -n "$ENV_NAME" python3 - <<'PY'
from datetime import datetime, timedelta
now = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
print((now - timedelta(days=(now.weekday() - 3) % 7)).strftime('%Y%m%d'))
PY
    )"
    log "[INFO] Resolved init date (latest Thursday): $INIT_DATE"
fi

# ---- Failure notification ---------------------------------------------------
notify_failure() {
    local exit_code=$?
    log "[ERROR] Workflow failed (exit $exit_code). Sending notification."
    conda run -n "$ENV_NAME" python3 - "$CONFIG" <<'PY' || true
import sys, yaml
from utils.utils_email import send_email
cfg = yaml.safe_load(open(sys.argv[1])) or {}
send_email(
    cfg.get("email", {}),
    "SubX pipeline failed",
    "The SubX cron workflow failed. Check logs/cron/ for details.",
)
PY
}
trap 'notify_failure' ERR

# ---- Run workflow -----------------------------------------------------------
log "[INFO] Running stages: $STAGES"
# shellcheck disable=SC2086
conda run --no-capture-output -n "$ENV_NAME" \
    python3 "$ROOT_DIR/runners/cli.py" \
    --system subx \
    --config "$CONFIG" \
    --init "$INIT_DATE" \
    --stages $STAGES

log "==> SubX cron run complete."
