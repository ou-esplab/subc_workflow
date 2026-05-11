#!/usr/bin/env bash
set -euo pipefail
trap 'code=$?; echo "[ERROR] run_addvars_rt.sh failed (exit $code) at line $LINENO"; exit $code' ERR

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

export PYTHONUNBUFFERED=1
PS4='+ $(date "+%F %T") run_addvars_rt.sh: '
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
al = cfg.get("arraylake") or {}

print(f"ENABLED|{1 if al.get('enabled', False) else 0}")
PY
)

AL_ENABLED=0
for line in "${CFG_LINES[@]}"; do
  IFS='|' read -r key value <<<"$line"
  case "$key" in
    ENABLED) AL_ENABLED="$value" ;;
  esac
done

if [[ "$AL_ENABLED" != "1" ]]; then
  echo "==> [run_addvars_rt] arraylake.enabled is false; skipping"
  exit 0
fi

echo "==> [run_addvars_rt] python=$(command -v python3)"
python3 -V || true

ENV_FILE="$SCRIPT_DIR/.env"
if [[ -f "$ENV_FILE" ]]; then
  # Load key=value pairs from arraylake/.env, including ARRAYLAKE_TOKEN.
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
else
  echo "[WARN] .env not found at $ENV_FILE; proceeding with existing environment" >&2
fi

CMD=(python3 "$SCRIPT_DIR/update_arraylake_fcsts.py" --config "$CONFIG")
if [[ -n "${FCSTDATE:-}" ]]; then
  CMD+=(--date "$FCSTDATE")
fi
if [[ "${ARRAYLAKE_DRY_RUN:-0}" == "1" ]]; then
  CMD+=(--dry-run)
fi

"${CMD[@]}"

echo "==> [run_addvars_rt] Done."
