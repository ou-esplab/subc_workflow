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
MODEL_FILTER="${3:-}"   # Optional GROUP-MODEL filter, e.g. ECCC-GEPS8
CONFIG="$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$CONFIG_IN")"

# Validate
if [[ -z "${FCSTDATE:-}" ]]; then
  echo "[FATAL] FCSTDATE is empty; usage: update_subx_fcsts.sh <FCSTDATE> [CONFIG] [GROUP-MODEL]" >&2
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
ingest_cfg = cfg.get("ingest") or {}
source_default = str(ingest_cfg.get("source_default", "iridl") or "iridl")
primary_enabled = bool(ingest_cfg.get("primary_enabled", True))
model_source = ingest_cfg.get("model_source") or {}
shadow_cfg = ingest_cfg.get("shadow") or {}
shadow_enabled = bool(shadow_cfg.get("enabled", False))
shadow_rt_root = str(shadow_cfg.get("rt_root", "") or "")
shadow_model_source = shadow_cfg.get("model_source") or {}

if shadow_enabled and not shadow_rt_root:
    raise SystemExit("ingest.shadow.rt_root is required when ingest.shadow.enabled=true")

print(f"MAXJOBS|{downloads}")
print(f"PRIMARY|{1 if primary_enabled else 0}")
print(f"SHADOW|{1 if shadow_enabled else 0}|{shadow_rt_root}")
for model in models:
    group = model["group"]
    server = model["name"]
    local = model_map.get(f"{group}-{server}", model_map.get(server, server))
    variables = ",".join(model.get("vars") or [])
    model_key = f"{group}-{server}"
    source = str(model_source.get(f"{group}-{server}", source_default) or source_default)
    print(f"MODEL|{group}|{server}|{local}|{variables}|{source}")
    if shadow_enabled:
        shadow_source = str(shadow_model_source.get(model_key, "") or "")
        if shadow_source:
            print(f"SHADOWMODEL|{group}|{server}|{local}|{variables}|{shadow_source}")
PY
)

MAXJOBS=1
MODEL_SPECS=()
SHADOW_MODEL_SPECS=()
PRIMARY_ENABLED=1
SHADOW_ENABLED=0
SHADOW_RT_ROOT=""
for line in "${CONFIG_LINES[@]}"; do
  IFS='|' read -r kind field1 field2 field3 field4 field5 <<<"$line"
  if [[ "$kind" == "MAXJOBS" ]]; then
    MAXJOBS="$field1"
  elif [[ "$kind" == "PRIMARY" ]]; then
    PRIMARY_ENABLED="$field1"
  elif [[ "$kind" == "SHADOW" ]]; then
    SHADOW_ENABLED="$field1"
    SHADOW_RT_ROOT="$field2"
  elif [[ "$kind" == "MODEL" ]]; then
    if [[ -z "$MODEL_FILTER" || "${field1}-${field2}" == "$MODEL_FILTER" ]]; then
      MODEL_SPECS+=("$line")
    fi
  elif [[ "$kind" == "SHADOWMODEL" ]]; then
    if [[ -z "$MODEL_FILTER" || "${field1}-${field2}" == "$MODEL_FILTER" ]]; then
      SHADOW_MODEL_SPECS+=("$line")
    fi
  fi
done

if [[ "$PRIMARY_ENABLED" == "1" && ${#MODEL_SPECS[@]} -eq 0 ]]; then
  if [[ -n "$MODEL_FILTER" ]]; then
    echo "[FATAL] Model filter '$MODEL_FILTER' matched no models in $CONFIG" >&2
  else
    echo "[FATAL] No model specifications were parsed from $CONFIG" >&2
  fi
  exit 2
fi

export SUBX_CONFIG="$CONFIG"

job_count=0
if [[ "$PRIMARY_ENABLED" == "1" ]]; then
  echo "==> [update_subx_fcsts] Downloading inputs for ${#MODEL_SPECS[@]} models, max jobs=$MAXJOBS"
  for spec in "${MODEL_SPECS[@]}"; do
    IFS='|' read -r _ group server_model local_model vars_csv ingest_source <<<"$spec"
    IFS=',' read -r -a vars <<<"$vars_csv"
    echo "==> [update_subx_fcsts] Model $group/$server_model -> $local_model source=$ingest_source vars=${vars[*]}"
    for var in "${vars[@]}"; do
      SUBX_INGEST_SOURCE="$ingest_source" stdbuf -oL -eL "$SCRIPT_DIR/download_subx_rtfcst.sh" \
        "$group" "$server_model" "$var" forecast "$FCSTDATE" "$local_model" &
      ((++job_count))
      if (( job_count % MAXJOBS == 0 )); then
        wait
      fi
    done
  done
  wait
else
  echo "==> [update_subx_fcsts] Primary ingest disabled (ingest.primary_enabled=false); skipping primary downloads"
fi

if [[ "$SHADOW_ENABLED" == "1" && ${#SHADOW_MODEL_SPECS[@]} -gt 0 ]]; then
  echo "==> [update_subx_fcsts] Shadow direct ingest enabled; root=$SHADOW_RT_ROOT"
  for spec in "${SHADOW_MODEL_SPECS[@]}"; do
    IFS='|' read -r _ group server_model local_model vars_csv ingest_source <<<"$spec"
    IFS=',' read -r -a vars <<<"$vars_csv"
    echo "==> [update_subx_fcsts] Shadow model $group/$server_model -> $local_model source=$ingest_source vars=${vars[*]}"
    for var in "${vars[@]}"; do
      RT_ROOT="$SHADOW_RT_ROOT" SUBX_INGEST_SOURCE="$ingest_source" stdbuf -oL -eL "$SCRIPT_DIR/download_subx_rtfcst.sh" \
        "$group" "$server_model" "$var" forecast "$FCSTDATE" "$local_model" &
      ((++job_count))
      if (( job_count % MAXJOBS == 0 )); then
        wait
      fi
    done
  done
  wait
fi

echo "==> [update_subx_fcsts] Done."