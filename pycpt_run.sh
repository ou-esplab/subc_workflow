#!/usr/bin/env bash
set -euo pipefail
trap 'code=$?; echo "[ERROR] pycpt_run.sh failed (exit $code) at line $LINENO"; exit $code' ERR

export PYTHONUNBUFFERED=1
PS4='+ $(date "+%F %T") pycpt_run.sh: '
set -x

FCSTDATE="${1:-}"
CONFIG_IN="${2:-config.yaml}"
CONFIG="$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$CONFIG_IN")"

# Resolve forecast date (latest Thursday if not provided)
if [[ -z "${FCSTDATE:-}" ]]; then
  FCSTDATE="$(
    python3 - <<'PY'
import sys,signal; signal.signal(signal.SIGPIPE, signal.SIG_DFL)
from datetime import datetime, timedelta
now=datetime.utcnow().replace(hour=0,minute=0,second=0,microsecond=0)
print((now - timedelta(days=(now.weekday()-3)%7)).strftime('%Y%m%d'))
PY
  )"
fi

if [[ -z "${CONFIG:-}" || ! -f "$CONFIG" ]]; then echo "[FATAL] CONFIG not found: '$CONFIG_IN' -> '$CONFIG'"; exit 2; fi
echo "==> [pycpt_run] Using FCSTDATE=$FCSTDATE CONFIG=$CONFIG"

# Regions (defensive; stderr surfaced)
readarray -t LINES < <(
python3 -c '
import sys, signal, yaml, traceback
signal.signal(signal.SIGPIPE, signal.SIG_DFL)
try:
    cfg=yaml.safe_load(open(sys.argv[1])) or {}
    def to_negpos(lons): return [ (lo-360 if (lo is not None and lo>180) else lo) for lo in lons ]
    for r in cfg.get("regions", []):
        name = r.get("name","<no-name>")
        p = r.get("pycpt") or {}
        lat, lon = p.get("lat"), p.get("lon")
        if not lat or not lon:
            subx = r.get("subx") or {}
            slat, slon = subx.get("lat"), subx.get("lon")
            if slat and len(slat)==2 and slon and len(slon)==2:
                lat, lon = slat, to_negpos(slon)
            else:
                lat, lon = [None,None], [None,None]
        print(f"{name}|{lat[0]} {lat[1]}|{lon[0]} {lon[1]}")
except Exception:
    traceback.print_exc()
    sys.exit(1)
' "$CONFIG" 2> >(sed 's/^/[PYERR] /' >&2)
)

SEAS="$(
python3 -c '
import sys, signal, yaml, traceback
signal.signal(signal.SIGPIPE, signal.SIG_DFL)
try:
    print((yaml.safe_load(open(sys.argv[1])) or {})["pycpt"]["training_season"])
except Exception:
    traceback.print_exc()
    sys.exit(1)
' "$CONFIG" 2> >(sed 's/^/[PYERR] /' >&2)
)"

echo "==> [pycpt_run] Regions to run:"
for line in "${LINES[@]}"; do echo "   $line"; done

# Check for smoke mode in config
PYCPT_SMOKE_MODE="$(
python3 -c '
import sys, signal, yaml, traceback
signal.signal(signal.SIGPIPE, signal.SIG_DFL)
try:
    cfg = yaml.safe_load(open(sys.argv[1])) or {}
    smoke = (cfg.get("pycpt") or {}).get("smoke_mode", False)
    print(1 if smoke else 0)
except Exception:
    traceback.print_exc()
    print(0)
' "$CONFIG" 2> >(sed 's/^/[PYERR] /' >&2)
)"

# Build pycpt_s2s_realtime.py command with optional smoke mode
PYCPT_OPTS=""
if [[ "$PYCPT_SMOKE_MODE" == "1" ]]; then
  echo "==> [pycpt_run] Running in SMOKE MODE (data validation only, no CPT evaluation)"
  PYCPT_OPTS="--smoke"
fi

for line in "${LINES[@]}"; do
  IFS='|' read -r REG LATS LONS <<<"$line"
  if [[ "$LATS" == "None None" || "$LONS" == "None None" ]]; then
    echo "[WARN] Skipping region '$REG' (missing CPT extents)"; continue
  fi
  echo "==> [pycpt_run] Running PyCPT for region '$REG' (LATS=$LATS LONS=$LONS SEAS=$SEAS)"
  stdbuf -oL -eL ./pycpt_s2s_realtime.py \
    --regname "$REG" \
    --lat_minmax $LATS \
    --lon_minmax $LONS \
    --training_season "$SEAS" \
    --fcstdate "$FCSTDATE" \
    --config "$CONFIG" \
    $PYCPT_OPTS
done

echo "==> [pycpt_run] Done."