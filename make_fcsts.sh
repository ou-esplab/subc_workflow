#!/usr/bin/env bash
set -euo pipefail
trap 'code=$?; echo "[ERROR] make_fcsts.sh failed (exit $code) at line $LINENO"; exit $code' ERR

export PYTHONUNBUFFERED=1
PS4='+ $(date "+%F %T") make_fcsts.sh: '
set -x

FCSTDATE="${1:-}"
CONFIG_IN="${2:-config.yaml}"
CONFIG="$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$CONFIG_IN")"

if [[ -z "${FCSTDATE:-}" ]]; then echo "[FATAL] FCSTDATE empty"; exit 2; fi
if [[ -z "${CONFIG:-}" || ! -f "$CONFIG" ]]; then echo "[FATAL] CONFIG not found: '$CONFIG_IN' -> '$CONFIG'"; exit 2; fi

echo "==> [make_fcsts] Start for FCSTDATE=$FCSTDATE CONFIG=$CONFIG"

SMOKE_PRODUCTS="${SUBX_PRODUCTS_SMOKE:-}"
if [[ -z "$SMOKE_PRODUCTS" ]]; then
    SMOKE_PRODUCTS="$((python3 - <<'PY' "$CONFIG"
import sys
import yaml

cfg = yaml.safe_load(open(sys.argv[1])) or {}
print(1 if (cfg.get("workflow") or {}).get("products_smoke", False) else 0)
PY
    ))"
fi

OUTW="$(
python3 -c '
import sys, signal, yaml, traceback
signal.signal(signal.SIGPIPE, signal.SIG_DFL)
try:
    with open(sys.argv[1], "r") as f: print((yaml.safe_load(f) or {})["paths"]["out_weekly"])
except Exception: traceback.print_exc(); sys.exit(1)
' "$CONFIG" 2> >(sed 's/^/[PYERR] /' >&2)
)"
OUTD="$(
python3 -c '
import sys, signal, yaml, traceback
signal.signal(signal.SIGPIPE, signal.SIG_DFL)
try:
    with open(sys.argv[1], "r") as f: print((yaml.safe_load(f) or {})["paths"]["out_daily"])
except Exception: traceback.print_exc(); sys.exit(1)
' "$CONFIG" 2> >(sed 's/^/[PYERR] /' >&2)
)"

mkdir -p "${OUTW}/${FCSTDATE}/"{images,data} "${OUTD}/${FCSTDATE}/data"
LOCK="${OUTW}/${FCSTDATE}/subxfcst.lock"; touch "$LOCK"
cleanup(){ rm -f "$LOCK"; }; trap cleanup EXIT

echo "==> [make_fcsts] Validation"
stdbuf -oL -eL python3 validate_realtime.py --config "$CONFIG" --fcstdate "$FCSTDATE" --outdir "${OUTW}/${FCSTDATE}/data"

echo "==> [make_fcsts] Forecast"
FORECAST_ARGS=(--config "$CONFIG" --fcstdate "$FCSTDATE" --save)
if [[ "$SMOKE_PRODUCTS" == "1" ]]; then
    echo "==> [make_fcsts] Running in smoke mode (allow empty realtime inputs)"
    FORECAST_ARGS+=(--allow-empty-input)
fi
stdbuf -oL -eL python3 forecast.py "${FORECAST_ARGS[@]}"

# Best effort perms
chgrp -R esplab "${OUTW}/${FCSTDATE}" || true
chmod -R 775  "${OUTW}/${FCSTDATE}" || true

echo "==> [make_fcsts] Done."