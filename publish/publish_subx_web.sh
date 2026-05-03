#!/usr/bin/env bash
set -euo pipefail
trap 'code=$?; echo "[ERROR] publish_subx_web.sh failed (exit $code) at line $LINENO"; exit $code' ERR

FCSTDATE="${1:-}"
CONFIG_IN="${2:-config.yaml}"
CONFIG="$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$CONFIG_IN")"

if [[ -z "${FCSTDATE:-}" ]]; then
  echo "[FATAL] FCSTDATE is empty; usage: publish_subx_web.sh <FCSTDATE> [CONFIG]" >&2
  exit 2
fi
if [[ -z "${CONFIG:-}" || ! -f "$CONFIG" ]]; then
  echo "[FATAL] CONFIG not found or empty: '$CONFIG_IN' -> '$CONFIG'" >&2
  exit 2
fi

OUTW="$(python3 - <<'PY' "$CONFIG"
import sys
import yaml
cfg = yaml.safe_load(open(sys.argv[1])) or {}
print((cfg.get("paths") or {}).get("out_weekly", ""))
PY
)"
if [[ -z "${OUTW:-}" ]]; then
  echo "[FATAL] paths.out_weekly missing in config: $CONFIG" >&2
  exit 2
fi

SOURCE_DIR="${OUTW}/${FCSTDATE}"
if [[ ! -d "${SOURCE_DIR}/images" || ! -d "${SOURCE_DIR}/data" ]]; then
  echo "[FATAL] Missing source directories: ${SOURCE_DIR}/images or ${SOURCE_DIR}/data" >&2
  exit 2
fi

DEST_HOST="${SUBX_WEB_HOST:-somclass23}"
DEST_BASE="${SUBX_WEB_BASE:-/home/kpegion/http/subc/forecasts}"
SSH_KEY="${SUBX_WEB_SSH_KEY:-$HOME/.ssh/id_ed25519}"
PUBLISH_SUBDIR="${SUBX_PUBLISH_SUBDIR:-}"
VERIFY_REMOTE="${SUBX_PUBLISH_VERIFY_REMOTE:-1}"

if [[ -n "$PUBLISH_SUBDIR" ]]; then
  IMAGE_DEST="${DEST_BASE}/images/${FCSTDATE}/${PUBLISH_SUBDIR}"
  DATA_DEST="${DEST_BASE}/data/${FCSTDATE}/${PUBLISH_SUBDIR}"
else
  IMAGE_DEST="${DEST_BASE}/images/${FCSTDATE}"
  DATA_DEST="${DEST_BASE}/data/${FCSTDATE}"
fi

echo "==> [publish] FCSTDATE=$FCSTDATE HOST=$DEST_HOST SUBDIR=${PUBLISH_SUBDIR:-<none>}"
echo "==> [publish] Creating remote destination directories"
ssh -i "$SSH_KEY" "$DEST_HOST" "mkdir -p '$IMAGE_DEST' '$DATA_DEST'"

if ! scp -i "$SSH_KEY" "${SOURCE_DIR}/images"/* "${DEST_HOST}:${IMAGE_DEST}/"; then
  echo "[FATAL] Image transfer failed: ${IMAGE_DEST}" >&2
  exit 3
fi

if ! scp -i "$SSH_KEY" "${SOURCE_DIR}/data"/* "${DEST_HOST}:${DATA_DEST}/"; then
  echo "[FATAL] Data transfer failed: ${DATA_DEST}" >&2
  exit 3
fi

if [[ "$VERIFY_REMOTE" == "1" ]]; then
  echo "==> [publish] Verifying remote file counts"
  ssh -i "$SSH_KEY" "$DEST_HOST" "echo images=\$(find '$IMAGE_DEST' -maxdepth 1 -type f | wc -l); echo data=\$(find '$DATA_DEST' -maxdepth 1 -type f | wc -l)"
fi

echo "==> [publish] Complete: ${IMAGE_DEST} and ${DATA_DEST}"
