#!/usr/bin/env bash
set -euo pipefail
trap 'code=$?; echo "[ERROR] update_subx_fcsts.sh failed (exit $code) at line $LINENO"; exit $code' ERR

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

export PYTHONUNBUFFERED=1
PS4='+ $(date "+%F %T") update_subx_fcsts.sh: '
set -x

FCSTDATE="${1:-}"
CONFIG_IN="${2:-config.yaml}"
CONFIG="$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$CONFIG_IN")"

# Validate
if [[ -z "${FCSTDATE:-}" ]]; then
  echo "[FATAL] FCSTDATE is empty; usage: update_subx_fcsts.sh <FCSTDATE> [CONFIG]" >&2
  exit 2
fi
if [[ -z "${CONFIG:-}" || ! -f "$CONFIG" ]]; then
  echo "[FATAL] CONFIG not found or empty: '$CONFIG_IN' -> '$CONFIG'" >&2
  exit 2
fi

echo "==> [update_subx_fcsts] Start for FCSTDATE=$FCSTDATE CONFIG=$CONFIG"
echo "==> PYTHON: $(command -v python3)"
readarray -t CONFIG_LINES < <(
  python3 - "$CONFIG" <<'PY' 2> >(sed 's/^/[PYERR] /' >&2)
import sys
import yaml

cfg = yaml.safe_load(open(sys.argv[1])) or {}
models = cfg.get("models") or []
if not isinstance(models, list) or not models:
    raise SystemExit("config.yaml must define a non-empty top-level 'models' list")

model_map = cfg.get("model_name_map") or {}
downloads = max(int((cfg.get("concurrency") or {}).get("downloads", 1)), 1)

print(f"MAXJOBS|{downloads}")
for model in models:
    group = model["group"]
    server = model["name"]
    local = model_map.get(f"{group}-{server}", model_map.get(server, server))
    variables = ",".join(model.get("vars") or [])
    print(f"MODEL|{group}|{server}|{local}|{variables}")
PY
)

MAXJOBS=1
MODEL_SPECS=()
for line in "${CONFIG_LINES[@]}"; do
  IFS='|' read -r kind field1 field2 field3 field4 <<<"$line"
  if [[ "$kind" == "MAXJOBS" ]]; then
    MAXJOBS="$field1"
  elif [[ "$kind" == "MODEL" ]]; then
    MODEL_SPECS+=("$line")
  fi
done

if [[ ${#MODEL_SPECS[@]} -eq 0 ]]; then
  echo "[FATAL] No model specifications were parsed from $CONFIG" >&2
  exit 2
fi

echo "==> [update_subx_fcsts] Downloading inputs for ${#MODEL_SPECS[@]} models, max jobs=$MAXJOBS"

export SUBX_CONFIG="$CONFIG"

job_count=0
for spec in "${MODEL_SPECS[@]}"; do
  IFS='|' read -r _ group server_model local_model vars_csv <<<"$spec"
  IFS=',' read -r -a vars <<<"$vars_csv"
  echo "==> [update_subx_fcsts] Model $group/$server_model -> $local_model vars=${vars[*]}"
  for var in "${vars[@]}"; do
    stdbuf -oL -eL "$SCRIPT_DIR/download_subx_rtfcst.sh" \
      "$group" "$server_model" "$var" forecast "$FCSTDATE" "$local_model" &
    ((++job_count))
    if (( job_count % MAXJOBS == 0 )); then
      wait
    fi
  done
done
wait

echo "==> [update_subx_fcsts] Done."