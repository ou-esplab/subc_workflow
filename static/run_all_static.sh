#!/usr/bin/env bash
# run_all_static.sh — Generate SubX climatology and percentile threshold files.
#
# Run this once (or when adding new models) before the realtime workflow.
# Climatologies are written to: {hc_root}/{var}{lev}/daily/climo/{group}-{model}/
# Percentile files are written to: {hc_root}/{var}{lev}/daily/percentiles/
#
# --start-year/--end-year are REQUIRED and set the hindcast reference period
# used for BOTH climatology and percentile computation (no auto-detection, no
# default -- see docs/README_climatology_reference_period.md for the current
# common-overlap-period value used across all models and why). Percentiles
# are self-computed from the same rt_root hindcast archive as climatology
# (via make_subx_percentiles.py), not repackaged from external GWSC files --
# this ensures the mean climatology and the exceedance/tercile thresholds
# always share one consistent reference period. Percentiles are only
# computed for pr/sfc and tas/2m (the only var/lev pairs any current
# exceedance product uses), not the full climatology var/lev matrix.
#
# Every run appends a record (reference period, percentile values, entries
# processed, git commit, timestamp) to {hc_root}/CLIMATOLOGY_MANIFEST.jsonl
# via static/record_climatology_run.py -- check that file, not file mtimes,
# to find out what period any existing climo/percentile file was built on.
#
# Usage:
#   ./run_all_static.sh --start-year YYYY --end-year YYYY [OPTIONS]
#
# Options:
#   --config PATH          Path to config.yaml (default: ../config.yaml)
#   --start-year YYYY       First hindcast year (REQUIRED)
#   --end-year YYYY         Last hindcast year (REQUIRED)
#   --percentiles "5,33,67,95"  Comma-separated percentile values (default: 5,33,67,95)
#   --window N              Pooling half-width in days for percentiles (default: 15)
#   --workers N              Parallel workers WITHIN one model/var/lev combo, for
#                            file-opening and dask-backed data loading (default: 8)
#   --parallel-combos N     How many model/var/lev (or /pct) combos to run
#                            CONCURRENTLY as separate background processes
#                            (default: 3). Total concurrent load is roughly
#                            parallel-combos x workers -- keep the product at or
#                            below the machine's CPU count (e.g. 3x8=24 on a
#                            26-core node). Each combo's own stdout/stderr goes to
#                            logs/climo_<model>_<var><lev>.log or
#                            logs/pct_<model>_<var><lev>_p<pct>.log so concurrent
#                            output doesn't interleave in the main log.
#   --group GROUP           Only process this group (e.g. ECCC)
#   --model MODEL           Only process this model (e.g. GEPS8); requires --group
#   --overwrite             Overwrite existing files
#   --climo-only            Skip percentile step
#   --pct-only               Skip climatology step
#   --dry-run                Preview without writing files
#
# Examples:
#   ./run_all_static.sh --start-year 2001 --end-year 2016
#   ./run_all_static.sh --start-year 2001 --end-year 2016 --group ECCC --model GEPS8
#
# Variable × level matrix (must match config.yaml models[].vars and models[].levels):
#   pr   sfc    rlut  sfc    tas  2m    ts   sfc    ua  200    va  200    zg  500

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

CONFIG="${ROOT_DIR}/config.yaml"
START_YEAR=""
END_YEAR=""
PERCENTILES="5,33,67,95"
WINDOW=15
WORKERS=8
PARALLEL_COMBOS=3
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
    --config)      CONFIG="$2";       shift 2 ;;
    --start-year)  START_YEAR="$2";   shift 2 ;;
    --end-year)    END_YEAR="$2";     shift 2 ;;
    --percentiles) PERCENTILES="$2";  shift 2 ;;
    --window)      WINDOW="$2";       shift 2 ;;
    --workers)     WORKERS="$2";      shift 2 ;;
    --parallel-combos) PARALLEL_COMBOS="$2"; shift 2 ;;
    --group)       FILTER_GROUP="$2"; shift 2 ;;
    --model)       FILTER_MODEL="$2"; shift 2 ;;
    --overwrite)   OVERWRITE="--overwrite"; shift ;;
    --climo-only)  RUN_PCT=0;  shift ;;
    --pct-only)    RUN_CLIMO=0; shift ;;
    --dry-run)     DRY_RUN="--dry-run"; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$START_YEAR" || -z "$END_YEAR" ]]; then
  echo "[ERROR] --start-year and --end-year are required (no default -- state the" >&2
  echo "        reference period explicitly every run)." >&2
  echo "Usage: $0 --start-year YYYY --end-year YYYY [OPTIONS]" >&2
  exit 1
fi

# Activate conda environment
set +u
# shellcheck source=/dev/null
source "$CONDA_BASE/etc/profile.d/conda.sh"
conda activate "$ENV_NAME"
set -u

log() { echo "[$(date -u +%F\ %T\ UTC)] $*"; }
log "Starting static file generation (config=$CONFIG, years=${START_YEAR}-${END_YEAR})"
[[ -n "$FILTER_GROUP" ]] && log "Filtering to group: $FILTER_GROUP"
[[ -n "$FILTER_MODEL" ]] && log "Filtering to model: $FILTER_MODEL"

HC_ROOT="$(python3 -c "import yaml,sys; print(yaml.safe_load(open(sys.argv[1]))['paths']['hc_root'])" "$CONFIG")"

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
  log "=== CLIMATOLOGIES (${START_YEAR}-${END_YEAR}, ${PARALLEL_COMBOS} concurrent combos) ==="
  mkdir -p "$ROOT_DIR/logs"
  declare -a CLIMO_DONE=()
  declare -a CLIMO_FAILED=()
  declare -a CLIMO_PIDS=()
  declare -a CLIMO_LABELS=()

  for entry in "${ENTRIES[@]}"; do
    read -r group model var lev <<< "$entry"
    [[ -n "$FILTER_GROUP" && "$group" != "$FILTER_GROUP" ]] && continue
    [[ -n "$FILTER_MODEL" && "$model" != "$FILTER_MODEL" ]] && continue

    # Throttle to at most PARALLEL_COMBOS concurrent jobs. `|| true` guards
    # against set -e: `wait -n` returns the exit status of whichever job it
    # reaped, so a failed background job here would otherwise abort the
    # whole script mid-throttle -- failures are instead collected properly
    # in the final wait loop below.
    while [[ $(jobs -r -p | wc -l) -ge $PARALLEL_COMBOS ]]; do
      wait -n || true
    done

    label="$group-$model $var $lev"
    joblog="$ROOT_DIR/logs/climo_${group}-${model}_${var}${lev}.log"
    log "Climatology: $label (background -> $joblog)"
    (
      python3 "$SCRIPT_DIR/make_subx_climo.py" \
        --config "$CONFIG" \
        --group  "$group"  \
        --model  "$model"  \
        --var    "$var"    \
        --lev    "$lev"    \
        --start-year "$START_YEAR" \
        --end-year   "$END_YEAR"   \
        --workers    "$WORKERS"    \
        ${OVERWRITE}
    ) > "$joblog" 2>&1 &
    CLIMO_PIDS+=($!)
    CLIMO_LABELS+=("$label")
  done

  for i in "${!CLIMO_PIDS[@]}"; do
    if wait "${CLIMO_PIDS[$i]}"; then
      CLIMO_DONE+=("${CLIMO_LABELS[$i]}")
    else
      log "[WARN] Failed: ${CLIMO_LABELS[$i]} (see logs/climo_*.log) (skipping)"
      CLIMO_FAILED+=("${CLIMO_LABELS[$i]}")
    fi
  done
  log "=== CLIMATOLOGIES COMPLETE ==="

  if [[ -z "$DRY_RUN" ]]; then
    DONE_STR="$(IFS=,; echo "${CLIMO_DONE[*]-}")"
    FAILED_STR="$(IFS=,; echo "${CLIMO_FAILED[*]-}")"
    python3 "$SCRIPT_DIR/record_climatology_run.py" \
      --hc-root "$HC_ROOT" --stage climo \
      --start-year "$START_YEAR" --end-year "$END_YEAR" \
      --entries "$DONE_STR" --failed-entries "$FAILED_STR"
  fi
fi

# ---------------------------------------------------------------------------
# Percentile threshold files -- self-computed from the same rt_root hindcast
# archive as climatology (make_subx_percentiles.py), scoped to pr/sfc and
# tas/2m only (the only var/lev pairs any exceedance product uses today).
# Reuses the ENTRIES matrix above, filtered to those two var/lev pairs, so
# model coverage automatically matches climatology (e.g. GEOS_V3 only has
# pr/tas anyway).
# ---------------------------------------------------------------------------
if [[ $RUN_PCT -eq 1 ]]; then
  log "=== PERCENTILE THRESHOLDS (${START_YEAR}-${END_YEAR}, pct=${PERCENTILES}, ${PARALLEL_COMBOS} concurrent) ==="
  mkdir -p "$ROOT_DIR/logs"
  IFS=',' read -r -a PCT_LIST <<< "$PERCENTILES"
  YEARS_LIST="$(seq "$START_YEAR" "$END_YEAR")"
  declare -a PCT_DONE=()
  declare -a PCT_FAILED=()
  declare -a PCT_PIDS=()
  declare -a PCT_LABELS=()

  for entry in "${ENTRIES[@]}"; do
    read -r group model var lev <<< "$entry"
    [[ -n "$FILTER_GROUP" && "$group" != "$FILTER_GROUP" ]] && continue
    [[ -n "$FILTER_MODEL" && "$model" != "$FILTER_MODEL" ]] && continue
    # Scope to pr/sfc and tas/2m only.
    if ! { [[ "$var" == "pr" && "$lev" == "sfc" ]] || [[ "$var" == "tas" && "$lev" == "2m" ]]; }; then
      continue
    fi
    for pct in "${PCT_LIST[@]}"; do
      # Same throttle-and-collect pattern as the climatology loop above.
      while [[ $(jobs -r -p | wc -l) -ge $PARALLEL_COMBOS ]]; do
        wait -n || true
      done

      label="$group-$model $var $lev.$pct"
      joblog="$ROOT_DIR/logs/pct_${group}-${model}_${var}${lev}_p${pct}.log"
      log "Percentiles: $label (background -> $joblog)"
      # shellcheck disable=SC2086
      (
        python3 "$SCRIPT_DIR/make_subx_percentiles.py" \
          --config     "$CONFIG"     \
          --model      "$group-$model" \
          --var        "$var"        \
          --lev        "$lev"        \
          --percentile "$pct"        \
          --years      $YEARS_LIST   \
          --window     "$WINDOW"     \
          --workers    "$WORKERS"    \
          ${DRY_RUN}                 \
          ${OVERWRITE}
      ) > "$joblog" 2>&1 &
      PCT_PIDS+=($!)
      PCT_LABELS+=("$label")
    done
  done

  for i in "${!PCT_PIDS[@]}"; do
    if wait "${PCT_PIDS[$i]}"; then
      PCT_DONE+=("${PCT_LABELS[$i]}")
    else
      log "[WARN] Failed: percentiles ${PCT_LABELS[$i]} (see logs/pct_*.log) (skipping)"
      PCT_FAILED+=("${PCT_LABELS[$i]}")
    fi
  done
  log "=== PERCENTILE THRESHOLDS COMPLETE ==="

  if [[ -z "$DRY_RUN" ]]; then
    DONE_STR="$(IFS=,; echo "${PCT_DONE[*]-}")"
    FAILED_STR="$(IFS=,; echo "${PCT_FAILED[*]-}")"
    python3 "$SCRIPT_DIR/record_climatology_run.py" \
      --hc-root "$HC_ROOT" --stage percentile \
      --start-year "$START_YEAR" --end-year "$END_YEAR" \
      --percentiles "$PERCENTILES" --window "$WINDOW" \
      --entries "$DONE_STR" --failed-entries "$FAILED_STR"
  fi
fi

log "run_all_static.sh done."
