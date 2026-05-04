#!/usr/bin/env bash
set -euo pipefail
trap 'code=$?; echo "[ERROR] pycpt_run.sh failed (exit $code) at line $LINENO"; exit $code' ERR

export PYTHONUNBUFFERED=1
PS4='+ $(date "+%F %T") pycpt_run.sh: '
set -x

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

FCSTDATE="${1:-}"
CONFIG_IN="${2:-config.yaml}"
CONFIG="$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$CONFIG_IN")"
PYCPT_ONLY_RAW="${PYCPT_ONLY:-}"
PYCPT_MAX_WORKERS="${PYCPT_MAX_WORKERS:-1}"
PYCPT_MODELS_RAW="${PYCPT_MODELS:-}"

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

# Regions for pycpt: use pycpt_regions if present, else all regions
readarray -t LINES < <(
python3 -c '
import sys, signal, yaml, traceback
signal.signal(signal.SIGPIPE, signal.SIG_DFL)
try:
  cfg=yaml.safe_load(open(sys.argv[1])) or {}
  only_raw = (sys.argv[2] or "").strip()
  only = set()
  if only_raw:
    for token in only_raw.replace(",", " ").split():
      token = token.strip()
      if token:
        only.add(token)
  def to_negpos(lons): return [ (lo-360 if (lo is not None and lo>180) else lo) for lo in lons ]
  region_names = cfg.get("pycpt_regions") or [r.get("name","<no-name>") for r in cfg.get("regions",[])]
  for r in cfg.get("regions", []):
    name = r.get("name","<no-name>")
    if name not in region_names:
      continue
    if only and name not in only:
      continue
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
' "$CONFIG" "$PYCPT_ONLY_RAW" 2> >(sed 's/^/[PYERR] /' >&2)
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

if ! [[ "$PYCPT_MAX_WORKERS" =~ ^[0-9]+$ ]] || [[ "$PYCPT_MAX_WORKERS" -lt 1 ]]; then
  echo "[FATAL] Invalid PYCPT_MAX_WORKERS='$PYCPT_MAX_WORKERS' (must be >=1 integer)" >&2
  exit 2
fi

if [[ "${#LINES[@]}" -eq 0 ]]; then
  echo "[WARN] No regions selected for PyCPT run."
  exit 0
fi

# Check for smoke mode in config
CONFIG_SMOKE_MODE="$(
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

# Optional env override from runner
if [[ -n "${PYCPT_SMOKE_MODE:-}" ]] && [[ "${PYCPT_SMOKE_MODE}" =~ ^[01]$ ]]; then
  FINAL_SMOKE_MODE="$PYCPT_SMOKE_MODE"
else
  FINAL_SMOKE_MODE="$CONFIG_SMOKE_MODE"
fi

# Build pycpt_s2s_realtime.py command with optional smoke mode
PYCPT_OPTS=""
if [[ "$FINAL_SMOKE_MODE" == "1" ]]; then
  echo "==> [pycpt_run] Running in SMOKE MODE (data validation only, no CPT evaluation)"
  PYCPT_OPTS="--smoke"
fi

run_region() {
  local line="$1"
  IFS='|' read -r REG LATS LONS <<<"$line"
  if [[ "$LATS" == "None None" || "$LONS" == "None None" ]]; then
    echo "[WARN] Skipping region '$REG' (missing CPT extents)"
    return 0
  fi
  echo "==> [pycpt_run] Running PyCPT for region '$REG' (LATS=$LATS LONS=$LONS SEAS=$SEAS)"
  local -a cmd=(
    python "$SCRIPT_DIR/pycpt_s2s_realtime.py"
    --regname "$REG"
    --lat_minmax $LATS
    --lon_minmax $LONS
    --training_season "$SEAS"
    --fcstdate "$FCSTDATE"
    --config "$CONFIG"
  )
  if [[ -n "$PYCPT_OPTS" ]]; then
    cmd+=("$PYCPT_OPTS")
  fi
  if [[ -n "$PYCPT_MODELS_RAW" ]]; then
    local models_csv="${PYCPT_MODELS_RAW//,/ }"
    read -r -a model_arr <<<"$models_csv"
    if [[ "${#model_arr[@]}" -gt 0 ]]; then
      cmd+=(--models "${model_arr[@]}")
      echo "==> [pycpt_run] Model override: ${model_arr[*]}"
    fi
  fi
  stdbuf -oL -eL "${cmd[@]}"
}

if [[ "$PYCPT_MAX_WORKERS" -eq 1 ]]; then
  for line in "${LINES[@]}"; do
    run_region "$line"
  done
else
  echo "==> [pycpt_run] Parallel PyCPT mode with max workers=$PYCPT_MAX_WORKERS"
  fail=0
  declare -a pids=()
  for line in "${LINES[@]}"; do
    run_region "$line" &
    pids+=("$!")
    while [[ "${#pids[@]}" -ge "$PYCPT_MAX_WORKERS" ]]; do
      if ! wait -n; then
        fail=1
      fi
      new_pids=()
      for pid in "${pids[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
          new_pids+=("$pid")
        fi
      done
      pids=("${new_pids[@]}")
    done
  done
  for pid in "${pids[@]}"; do
    if ! wait "$pid"; then
      fail=1
    fi
  done
  if [[ "$fail" -ne 0 ]]; then
    echo "[ERROR] One or more parallel PyCPT region jobs failed" >&2
    exit 1
  fi
fi

echo "==> [pycpt_run] Done."