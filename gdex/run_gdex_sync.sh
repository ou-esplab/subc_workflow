#!/usr/bin/env bash
set -euo pipefail
trap 'code=$?; echo "[ERROR] run_gdex_sync.sh failed (exit $code) at line $LINENO"; exit $code' ERR

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

export PYTHONUNBUFFERED=1
PS4='+ $(date "+%F %T") run_gdex_sync.sh: '
set -x

FCSTDATE="${1:-}"
CONFIG_IN="${2:-config.yaml}"
CONFIG="$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$CONFIG_IN")"

if [[ -z "${CONFIG:-}" || ! -f "$CONFIG" ]]; then
  echo "[FATAL] CONFIG not found: '$CONFIG_IN' -> '$CONFIG'" >&2
  exit 2
fi

readarray -t CFG_LINES < <(
python3 - "$CONFIG" <<'PY'
import sys
import yaml

cfg = yaml.safe_load(open(sys.argv[1])) or {}
gd = cfg.get("gdex") or {}

print(f"ENABLED|{1 if gd.get('enabled', False) else 0}")
PY
)

GDEX_ENABLED=0
for line in "${CFG_LINES[@]}"; do
  IFS='|' read -r key value <<<"$line"
  case "$key" in
    ENABLED) GDEX_ENABLED="$value" ;;
  esac
done

if [[ "$GDEX_ENABLED" != "1" ]]; then
  echo "==> [run_gdex_sync] gdex.enabled is false; skipping"
  exit 0
fi

echo "==> [run_gdex_sync] python=$(command -v python3)"
python3 -V || true

# globus-cli lives in its own dedicated conda env (SUBC_GDEX_ENV), not
# subc_workflow_env -- its OAuth token cache needs Python's sqlite3 module,
# which is broken in subc_workflow_env (pre-existing, unrelated conda
# package conflict). gdex_utils.py routes every `globus` call through
# `conda run -n "$SUBC_GDEX_ENV"` when this is set.
export SUBC_GDEX_ENV="${SUBC_GDEX_ENV:-subc_globus_env}"
if ! conda run -n "$SUBC_GDEX_ENV" command -v globus >/dev/null 2>&1; then
  echo "[FATAL] globus-cli not found in conda env '$SUBC_GDEX_ENV'; create it and 'pip install globus-cli' + 'globus login' first" >&2
  exit 2
fi

CMD=(python3 "$SCRIPT_DIR/sync_forecast_to_gdex.py" --config "$CONFIG")
if [[ -n "${FCSTDATE:-}" ]]; then
  CMD+=(--date "$FCSTDATE")
fi
if [[ "${GDEX_DRY_RUN:-0}" == "1" ]]; then
  CMD+=(--dry-run)
fi

"${CMD[@]}"

echo "==> [run_gdex_sync] Done."
