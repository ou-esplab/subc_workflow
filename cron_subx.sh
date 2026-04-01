#!/usr/bin/env bash
# cron_subx.sh — weekly SubX forecast workflow cron wrapper
#
# Intended crontab entry (runs every Thursday at 18:00 UTC):
#   0 18 * * 4  /home/kpegion/projects/subc_workflow/cron_subx.sh >> /home/kpegion/projects/subc_workflow/logs/cron.log 2>&1
#
# Optional overrides via environment variables:
#   SUBX_CONFIG   — path to config YAML (default: config.yaml next to this script)
#   SUBX_INIT     — override forecast init date as YYYYMMDD (default: latest Thursday)
#   SUBX_STAGES   — space-separated stage list (default: "ingest products pycpt")
#   CONDA_BASE    — path to miniconda/anaconda root (default: ~/miniconda3)
#   ENV_NAME      — conda environment name (default: subc_workflow_env)

set -euo pipefail

# ---- Resolve script location ------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---- Configuration ----------------------------------------------------------
CONFIG="${SUBX_CONFIG:-$SCRIPT_DIR/config.yaml}"
STAGES="${SUBX_STAGES:-ingest products pycpt publish}"
CONDA_BASE="${CONDA_BASE:-$HOME/miniconda3}"
ENV_NAME="${ENV_NAME:-subc_workflow_env}"
LOCK_FILE="/tmp/subx_cron.lock"

# ---- Logging ----------------------------------------------------------------
TS="$(date -u +%Y%m%d_%H%M%S)"
LOG_DIR="$SCRIPT_DIR/logs/cron"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/subx_${TS}.log"

log() { echo "[$(date -u +%F\ %T\ UTC)] $*" | tee -a "$LOG_FILE"; }

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

# ---- Conda environment ------------------------------------------------------
CONDA_SH="$CONDA_BASE/etc/profile.d/conda.sh"
if [[ ! -f "$CONDA_SH" ]]; then
    log "[FATAL] conda.sh not found at $CONDA_SH. Set CONDA_BASE."
    exit 2
fi
# shellcheck source=/dev/null
source "$CONDA_SH"
conda activate "$ENV_NAME"
log "[INFO] Activated conda env: $ENV_NAME (python=$(python3 -V 2>&1))"

# ---- Resolve forecast date --------------------------------------------------
if [[ -n "${SUBX_INIT:-}" ]]; then
    INIT_DATE="$SUBX_INIT"
    log "[INFO] Using provided init date: $INIT_DATE"
else
    INIT_DATE="$(python3 - <<'PY'
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
    python3 - "$CONFIG" <<'PY' || true
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
python3 "$SCRIPT_DIR/runners/cli.py" \
    --system subx \
    --config "$CONFIG" \
    --init "$INIT_DATE" \
    --stages $STAGES

log "==> SubX cron run complete."
