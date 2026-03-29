#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

GROUP="${1:?group is required}"
SERVER_MODEL="${2:?server model is required}"
VAR="${3:?variable is required}"
DATATYPE="${4:?datatype is required}"
FCST="${5:?forecast date is required}"
LOCAL_MODEL="${6:-$SERVER_MODEL}"

resolve_rt_root() {
  if [[ -n "${RT_ROOT:-}" ]]; then
    printf '%s\n' "$RT_ROOT"
    return 0
  fi

  if [[ -n "${SUBX_CONFIG:-}" && -f "$SUBX_CONFIG" ]]; then
    python3 - "$SUBX_CONFIG" <<'PY'
import sys
import yaml

cfg = yaml.safe_load(open(sys.argv[1])) or {}
print((cfg.get("paths") or {}).get("rt_root", ""))
PY
    return 0
  fi

  echo ""
}

RT_ROOT="$(resolve_rt_root)"
TARGET_DIR="${RT_ROOT}/${GROUP}-${LOCAL_MODEL}/${DATATYPE}/${VAR}"
REQUEST_FILE="${TARGET_DIR}/${VAR}_${GROUP}-${LOCAL_MODEL}_${FCST}.download-request.json"

if [[ -z "$RT_ROOT" ]]; then
  echo "[FATAL] Unable to resolve paths.rt_root. Export RT_ROOT or SUBX_CONFIG before running the downloader." >&2
  exit 2
fi

mkdir -p "$TARGET_DIR"
echo "[DL] group=$GROUP server_model=$SERVER_MODEL local_model=$LOCAL_MODEL var=$VAR type=$DATATYPE fcst=$FCST"

if [[ -n "${SUBX_DOWNLOAD_BIN:-}" ]]; then
  export RT_ROOT
  exec "$SUBX_DOWNLOAD_BIN" "$GROUP" "$SERVER_MODEL" "$VAR" "$DATATYPE" "$FCST" "$LOCAL_MODEL"
fi

if [[ "${SUBX_DOWNLOAD_STUB:-0}" == "1" ]]; then
  cat >"$REQUEST_FILE" <<EOF
{
  "group": "$GROUP",
  "server_model": "$SERVER_MODEL",
  "local_model": "$LOCAL_MODEL",
  "variable": "$VAR",
  "datatype": "$DATATYPE",
  "forecast_date": "$FCST",
  "target_dir": "$TARGET_DIR",
  "mode": "stub"
}
EOF
  echo "[DL] Stubbed download request written to $REQUEST_FILE"
  exit 0
fi

echo "[FATAL] No concrete download implementation is configured." >&2
echo "[FATAL] Set SUBX_DOWNLOAD_BIN to an executable downloader, or SUBX_DOWNLOAD_STUB=1 for workflow smoke tests." >&2
exit 2

