#!/usr/bin/env bash
set -euo pipefail
trap 'code=$?; echo "[ERROR] run_preprocess.sh failed (exit $code) at line $LINENO"; exit $code' ERR

export PYTHONUNBUFFERED=1
PS4='+ $(date "+%F %T") run_preprocess.sh: '
set -x

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

FCSTDATE="${1:-}"
CONFIG_IN="${2:-config.yaml}"
CONFIG="$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$CONFIG_IN")"

if [[ -z "${FCSTDATE:-}" ]]; then echo "[FATAL] FCSTDATE empty"; exit 2; fi
if [[ -z "${CONFIG:-}" || ! -f "$CONFIG" ]]; then echo "[FATAL] CONFIG not found: '$CONFIG_IN' -> '$CONFIG'"; exit 2; fi

OUTW="$(
python3 -c '
import sys, signal, yaml, traceback
signal.signal(signal.SIGPIPE, signal.SIG_DFL)
try:
    with open(sys.argv[1], "r") as f: print((yaml.safe_load(f) or {})["paths"]["out_weekly"])
except Exception: traceback.print_exc(); sys.exit(1)
' "$CONFIG" 2> >(sed 's/^/[PYERR] /' >&2)
)"

mkdir -p "${OUTW}/${FCSTDATE}/data"

echo "==> [run_preprocess] Validation for FCSTDATE=$FCSTDATE"
stdbuf -oL -eL python3 "$SCRIPT_DIR/validate_realtime.py" \
    --config "$CONFIG" \
    --fcstdate "$FCSTDATE" \
    --outdir "${OUTW}/${FCSTDATE}/data"

echo "==> [run_preprocess] Done."
