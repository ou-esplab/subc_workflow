#!/usr/bin/env bash
# run_all_static.sh — Generate SubX climatology and percentile threshold files.
#
# Run this once (or when adding new models) before the realtime workflow.
# Climatologies are written to: {hc_root}/{var}{lev}/daily/climo/{group}-{model}/
# Percentile files are written to: {hc_root}/{var}{lev}/daily/percentiles/
#
# Usage:
#   ./run_all_static.sh [OPTIONS]
#
# Options:
#   --config PATH          Path to config.yaml (default: ../config.yaml)
#   --group GROUP          Only process this group (e.g. ECCC)
#   --model MODEL          Only process this model (e.g. GEPS8); requires --group
#   --overwrite            Overwrite existing files
#   --climo-only           Skip percentile step
#   --pct-only             Skip climatology step
#   --dry-run              Preview without writing files
#
# Examples:
#   ./run_all_static.sh                              # all models
#   ./run_all_static.sh --group ECCC --model GEPS8  # one model
#   ./run_all_static.sh --group EMC                 # all EMC models
#
# Variable × level matrix (must match config.yaml models[].vars and models[].levels):
#   pr   sfc    rlut  sfc    tas  2m    ts   sfc    ua  200    va  200    zg  500

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

CONFIG="${ROOT_DIR}/config.yaml"
FILTER_GROUP=""
FILTER_MODEL=""
OVERWRITE=""
RUN_CLIMO=1
RUN_PCT=1
DRY_RUN=""
ENV_NAME="${ENV_NAME:-subc_workflow_env}"
CONDA_BASE="${CONDA_BASE:-$HOME/miniconda3}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)     CONFIG="$2";       shift 2 ;;
    --group)      FILTER_GROUP="$2"; shift 2 ;;
    --model)      FILTER_MODEL="$2"; shift 2 ;;
    --overwrite)  OVERWRITE="--overwrite"; shift ;;
    --climo-only) RUN_PCT=0;  shift ;;
    --pct-only)   RUN_CLIMO=0; shift ;;
    --dry-run)    DRY_RUN="--dry-run"; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

# Activate conda environment
set +u
# shellcheck source=/dev/null
source "$CONDA_BASE/etc/profile.d/conda.sh"
conda activate "$ENV_NAME"
set -u

log() { echo "[$(date -u +%F\ %T\ UTC)] $*"; }
log "Starting static file generation (config=$CONFIG)"
[[ -n "$FILTER_GROUP" ]] && log "Filtering to group: $FILTER_GROUP"
[[ -n "$FILTER_MODEL" ]] && log "Filtering to model: $FILTER_MODEL"

# ---------------------------------------------------------------------------
# Model × variable × level matrix
# Format: "GROUP MODEL VAR LEV"
# ---------------------------------------------------------------------------
declare -a ENTRIES=(
  "ECCC  GEPS8            pr   sfc"
  "ECCC  GEPS8            rlut sfc"
  "ECCC  GEPS8            tas  2m"
  "ECCC  GEPS8            ts   sfc"
  "ECCC  GEPS8            ua   200"
  "ECCC  GEPS8            va   200"
  "ECCC  GEPS8            zg   500"

  "EMC   GEFSv12_CPC      pr   sfc"
  "EMC   GEFSv12_CPC      tas  2m"
  "EMC   GEFSv12_CPC      zg   500"

  "ESRL  FIMr1p1          pr   sfc"
  "ESRL  FIMr1p1          rlut sfc"
  "ESRL  FIMr1p1          tas  2m"
  "ESRL  FIMr1p1          ts   sfc"
  "ESRL  FIMr1p1          ua   200"
  "ESRL  FIMr1p1          va   200"
  "ESRL  FIMr1p1          zg   500"

  "GMAO  GEOS_V2p1_5daily pr   sfc"
  "GMAO  GEOS_V2p1_5daily rlut sfc"
  "GMAO  GEOS_V2p1_5daily tas  2m"
  "GMAO  GEOS_V2p1_5daily ts   sfc"
  "GMAO  GEOS_V2p1_5daily ua   200"
  "GMAO  GEOS_V2p1_5daily va   200"
  "GMAO  GEOS_V2p1_5daily zg   500"

  # GEOS_V3's hindcast portal only provides pr/tas (see
  # static/download_geos_v3_hindcast.py) -- no rlut/ts/ua/va/zg entries here,
  # unlike every other model, because that data doesn't exist to compute a
  # climatology from.
  "GMAO  GEOS_V3          pr   sfc"
  "GMAO  GEOS_V3          tas  2m"

  "NCEP  CFSv2            pr   sfc"
  "NCEP  CFSv2            tas  2m"
  "NCEP  CFSv2            zg   500"

  "RSMAS CCSM4            pr   sfc"
  "RSMAS CCSM4            rlut sfc"
  "RSMAS CCSM4            tas  2m"
  "RSMAS CCSM4            ts   sfc"
  "RSMAS CCSM4            ua   200"
  "RSMAS CCSM4            va   200"
  "RSMAS CCSM4            zg   500"
)

# ---------------------------------------------------------------------------
# Climatologies
# ---------------------------------------------------------------------------
if [[ $RUN_CLIMO -eq 1 ]]; then
  log "=== CLIMATOLOGIES ==="
  for entry in "${ENTRIES[@]}"; do
    read -r group model var lev <<< "$entry"
    [[ -n "$FILTER_GROUP" && "$group" != "$FILTER_GROUP" ]] && continue
    [[ -n "$FILTER_MODEL" && "$model" != "$FILTER_MODEL" ]] && continue
    log "Climatology: $group-$model $var$lev"
    python3 "$SCRIPT_DIR/make_subx_climo.py" \
      --config "$CONFIG" \
      --group  "$group"  \
      --model  "$model"  \
      --var    "$var"    \
      --lev    "$lev"    \
      ${OVERWRITE}       \
      || log "[WARN] Failed: $group-$model $var$lev (skipping)"
  done
  log "=== CLIMATOLOGIES COMPLETE ==="
fi

# ---------------------------------------------------------------------------
# Percentile threshold files (split from GWSC source files)
# Only needs var+lev; script auto-discovers models from filenames
# ---------------------------------------------------------------------------
if [[ $RUN_PCT -eq 1 ]]; then
  log "=== PERCENTILE THRESHOLDS ==="
  declare -a VAR_LEVS=(
    "pr  sfc"
    "tas 2m"
    "zg  500"
  )
  for vl in "${VAR_LEVS[@]}"; do
    read -r var lev <<< "$vl"
    log "Percentiles: $var$lev"
    python3 "$SCRIPT_DIR/split_subx_percentiles.py" \
      --config  "$CONFIG" \
      --var     "$var"    \
      --lev     "$lev"    \
      ${DRY_RUN}          \
      ${OVERWRITE}        \
      || log "[WARN] Failed: percentiles $var$lev (skipping)"
  done
  log "=== PERCENTILE THRESHOLDS COMPLETE ==="
fi

log "run_all_static.sh done."
