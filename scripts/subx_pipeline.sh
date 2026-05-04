#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

ENV_NAME="${ENV_NAME:-subc_workflow_env}"
ENV_PREFIX="${ENV_PREFIX:-}"
CONDA_EXE="${CONDA_EXE:-$HOME/miniconda3/bin/conda}"
MAMBA_EXE="${MAMBA_EXE:-}"
MICROMAMBA_EXE="${MICROMAMBA_EXE:-}"
SKIP_ENV_ACTIVATION="${SKIP_ENV_ACTIVATION:-0}"

activate_failed() {
  echo "[FATAL] Failed to activate environment (ENV_NAME='$ENV_NAME' ENV_PREFIX='$ENV_PREFIX')" >&2
  exit 2
}

activate_environment() {
  if [[ "$SKIP_ENV_ACTIVATION" == "1" ]]; then
    echo "[INFO] Skipping environment activation (SKIP_ENV_ACTIVATION=1)"
    return 0
  fi

  set +u
  if [[ -n "$MAMBA_EXE" && -x "$MAMBA_EXE" ]]; then
    eval "$($MAMBA_EXE shell hook --shell bash)"
    if [[ -n "$ENV_PREFIX" ]]; then mamba activate "$ENV_PREFIX" || activate_failed
    else mamba activate "$ENV_NAME" || activate_failed
    fi
  elif [[ -x "$CONDA_EXE" ]]; then
    eval "$($CONDA_EXE shell.bash hook)"
    if [[ -n "$ENV_PREFIX" ]]; then conda activate "$ENV_PREFIX" || activate_failed
    else conda activate "$ENV_NAME" || activate_failed
    fi
  elif [[ -n "$MICROMAMBA_EXE" && -x "$MICROMAMBA_EXE" ]]; then
    eval "$($MICROMAMBA_EXE shell hook -s bash)"
    if [[ -n "$ENV_PREFIX" ]]; then micromamba activate "$ENV_PREFIX" || activate_failed
    else micromamba activate "$ENV_NAME" || activate_failed
    fi
  else
    echo "[FATAL] No conda/mamba/micromamba found. Set CONDA_EXE, MAMBA_EXE, or MICROMAMBA_EXE, or export SKIP_ENV_ACTIVATION=1." >&2
    exit 2
  fi
  set -u

  : "${CPT_BIN_DIR:=${CONDA_PREFIX:-$HOME/miniconda3}/bin}"
  echo "[INFO] Activated environment. python3=$(command -v python3)"
  python3 -V || true
}

notify_failure() {
  python3 - "$CONFIG" <<'PY'
import sys
import yaml

from utils.utils_email import send_email

cfg = yaml.safe_load(open(sys.argv[1])) or {}
send_email(cfg.get("email", {}), "SubX pipeline failed", "A stage failed. Check the pipeline logs for details.")
PY
}

activate_environment

FCSTDATE="${1:-}"
CONFIG_IN="${2:-config.yaml}"
CONFIG="$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$CONFIG_IN")"

if [[ -z "${CONFIG:-}" || ! -f "$CONFIG" ]]; then
  echo "[FATAL] CONFIG not found or empty: '$CONFIG_IN' -> '$CONFIG'" >&2
  exit 2
fi

if [[ -z "$FCSTDATE" ]]; then
  FCSTDATE="$(python3 - <<'PY'
from datetime import datetime, timedelta

now = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
print((now - timedelta(days=(now.weekday() - 3) % 7)).strftime('%Y%m%d'))
PY
  )"
fi

trap 'notify_failure' ERR

echo "[INFO] Delegating to runners/cli.py for FCSTDATE=$FCSTDATE CONFIG=$CONFIG"
python3 "$ROOT_DIR/runners/cli.py" --system subx --config "$CONFIG" --init "$FCSTDATE"