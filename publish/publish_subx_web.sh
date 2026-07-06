#!/usr/bin/env bash
set -euo pipefail
trap 'code=$?; echo "[ERROR] publish_subx_web.sh failed (exit $code) at line $LINENO"; exit $code' ERR

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_INDEX="$SCRIPT_DIR/web/index.html"

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

DEST_BASE="${SUBX_WEB_BASE:-/home/kpegion/http/subc/forecasts}"
SSH_KEY="${SUBX_WEB_SSH_KEY:-$HOME/.ssh/id_ed25519}"
PUBLISH_SUBDIR="${SUBX_PUBLISH_SUBDIR:-}"

if [[ -n "${SUBX_WEB_HOST:-}" ]]; then
  DEST_HOST="$SUBX_WEB_HOST"
else
  PRIMARY_HOST="somclass22"
  FALLBACK_HOST="somclass23"
  if ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=5 "$PRIMARY_HOST" true 2>/dev/null; then
    DEST_HOST="$PRIMARY_HOST"
  else
    echo "[WARN] $PRIMARY_HOST unreachable; falling back to $FALLBACK_HOST" >&2
    DEST_HOST="$FALLBACK_HOST"
  fi
fi
VERIFY_REMOTE="${SUBX_PUBLISH_VERIFY_REMOTE:-1}"
UPDATE_IMAGE_LATEST="${SUBX_PUBLISH_UPDATE_IMAGE_LATEST:-1}"

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

if ! scp -r -i "$SSH_KEY" "${SOURCE_DIR}/images"/* "${DEST_HOST}:${IMAGE_DEST}/"; then
  echo "[FATAL] Image transfer failed: ${IMAGE_DEST}" >&2
  exit 3
fi

if ! scp -r -i "$SSH_KEY" "${SOURCE_DIR}/data"/* "${DEST_HOST}:${DATA_DEST}/"; then
  echo "[FATAL] Data transfer failed: ${DATA_DEST}" >&2
  exit 3
fi

if [[ "$VERIFY_REMOTE" == "1" ]]; then
  echo "==> [publish] Verifying remote file counts"
  ssh -i "$SSH_KEY" "$DEST_HOST" "printf 'images='; find '$IMAGE_DEST' -maxdepth 1 -type f | wc -l; printf 'data='; find '$DATA_DEST' -maxdepth 1 -type f | wc -l"
fi

if [[ "$UPDATE_IMAGE_LATEST" == "1" ]]; then
  if [[ -n "$PUBLISH_SUBDIR" ]]; then
    IMAGE_LATEST_DEST="${DEST_BASE}/images/Latest/${PUBLISH_SUBDIR}"
  else
    IMAGE_LATEST_DEST="${DEST_BASE}/images/Latest"
  fi
  echo "==> [publish] Updating Latest images: ${IMAGE_LATEST_DEST}"
  ssh -i "$SSH_KEY" "$DEST_HOST" "sh -lc 'set -e; mkdir -p \"$IMAGE_LATEST_DEST\"; find \"$IMAGE_LATEST_DEST\" -mindepth 1 -maxdepth 1 -exec rm -rf {} +; cp -a \"$IMAGE_DEST\"/* \"$IMAGE_LATEST_DEST\"/'"
fi

UPDATE_DATE_DROPDOWN="${SUBX_PUBLISH_UPDATE_DATE_DROPDOWN:-1}"
if [[ "$UPDATE_DATE_DROPDOWN" == "1" ]]; then
  FCST_INDEX="${DEST_BASE}/index.html"
  if [[ ! -f "$LOCAL_INDEX" ]]; then
    echo "[FATAL] Local index.html not found: $LOCAL_INDEX" >&2
    exit 2
  fi
  # index.html is tracked in this repo (publish/web/index.html) as the source
  # of truth; update it locally, then deploy the result to the web host below.
  # Do not edit the remote copy directly -- changes made there won't survive
  # the next publish run, since this always overwrites it from the local file.
  echo "==> [publish] Adding ${FCSTDATE} to date dropdown in ${LOCAL_INDEX}"
  python3 - "$LOCAL_INDEX" "$FCSTDATE" <<'PYEOF'
import sys, re

index_file = sys.argv[1]
fcstdate = sys.argv[2]

with open(index_file, 'r') as f:
    content = f.read()

changed = False

label = '    <label for="fcstselectdate">Initialization Date</label>\n'
select_pattern = r'(\s*<select class="form-control" id="fcstselectdate">)'
if 'Initialization Date' not in content:
  content = re.sub(select_pattern, label + r'\1', content, count=1)
  changed = True

default_img_pattern = r'(<img[^>]*id="fcstImage"[^>]*src=")images/[^"]+("[^>]*>)'
new_content = re.sub(default_img_pattern, r'\1images/Latest/2mTempGlobalWeeks34.png\2', content, count=1)
if new_content != content:
  content = new_content
  changed = True

legacy_line = "    var text = 'images/' + date + '/' + varVal + region + lead + '.png';"
if legacy_line in content:
  content = content.replace(
    legacy_line,
    "    var name = varVal;\n"
    "    if (varVal !== \"500hPaGeopotentialHeightNorthernHemisphere\") {\n"
    "      name = varVal + region;\n"
    "    }\n"
    "    var text = 'images/' + date + '/' + name + lead + '.png';",
    1,
  )
  changed = True

autoload_block = "  // Initialize regions on load\n  updateRegions();"
if autoload_block in content and "  myFunction();" not in content:
  content = content.replace(autoload_block, autoload_block + "\n  myFunction();", 1)
  changed = True

# Insert new date option after the closing tag of the Latest option block
if fcstdate in content:
  print(f"[publish] Date {fcstdate} already in dropdown, skipping")
else:
  new_option = f"     <option>\n      {fcstdate}\n     </option>\n"
  pattern = r'(<option>\s*Latest\s*</option>)'
  replacement = r'\1\n' + new_option.rstrip('\n')
  new_content = re.sub(pattern, replacement, content, count=1)

  if new_content == content:
    print(f"[publish] WARNING: Could not find Latest option to insert after")
    sys.exit(1)
  content = new_content
  changed = True
  print(f"[publish] Date {fcstdate} added to dropdown")

if changed:
  with open(index_file, 'w') as f:
    f.write(content)
else:
  print("[publish] Index already up to date")
PYEOF

  echo "==> [publish] Deploying ${LOCAL_INDEX} to ${DEST_HOST}:${FCST_INDEX}"
  if ! scp -i "$SSH_KEY" "$LOCAL_INDEX" "${DEST_HOST}:${FCST_INDEX}"; then
    echo "[FATAL] Failed to deploy index.html to ${DEST_HOST}:${FCST_INDEX}" >&2
    exit 3
  fi
fi

echo "==> [publish] Complete: ${IMAGE_DEST} and ${DATA_DEST}"
