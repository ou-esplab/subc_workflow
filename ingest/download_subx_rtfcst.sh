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
if [[ -z "$RT_ROOT" ]]; then
  echo "[FATAL] Unable to resolve paths.rt_root. Export RT_ROOT or SUBX_CONFIG before running downloader." >&2
  exit 2
fi

TARGET_DIR="${RT_ROOT}/${GROUP}-${LOCAL_MODEL}/${DATATYPE}/${VAR}"
mkdir -p "$TARGET_DIR"

echo "[DL] group=$GROUP server_model=$SERVER_MODEL local_model=$LOCAL_MODEL var=$VAR type=$DATATYPE fcst=$FCST"

if [[ "${SUBX_DOWNLOAD_STUB:-0}" == "1" ]]; then
  marker_file="${TARGET_DIR}/${VAR}_${GROUP}-${LOCAL_MODEL}_${FCST}.download-request.json"
  cat > "$marker_file" <<EOF
{"mode":"stub","group":"$GROUP","server_model":"$SERVER_MODEL","local_model":"$LOCAL_MODEL","var":"$VAR","datatype":"$DATATYPE","fcst":"$FCST"}
EOF
  echo "[DL] Stub mode enabled. Wrote marker: $marker_file"
  exit 0
fi

BASE_URL="https://iridl.ldeo.columbia.edu/SOURCES/.Models/.SubC"
DATASET_URL="${BASE_URL}/.${GROUP}/.${SERVER_MODEL}/.${DATATYPE}/.${VAR}/dods/"

# Match the package repo flow: evaluate available IC dates and download those in
# the weekly window leading into FCST (inclusive).
START_WINDOW="$(date -d "${FCST} -7 days" +%Y%m%d)"
END_WINDOW="$FCST"

mapfile -t START_DATES < <(
  current="$START_WINDOW"
  while [[ "$current" -le "$END_WINDOW" ]]; do
    printf '%s\n' "$current"
    current="$(date -d "${current} +1 day" +%Y%m%d)"
  done
)

echo "[DL] Fetching metadata from $DATASET_URL"
METADATA="$(python3 "$SCRIPT_DIR/../utils/get_subx_metadata.py" "$DATASET_URL")"

mapfile -t AVAIL_DATES < <(
  printf '%s\n' "$METADATA" | awk '/^DATE /{print substr($2,1,8)}' | sort -u
)

if [[ ${#AVAIL_DATES[@]} -eq 0 ]]; then
  echo "[WARN] No start dates found in remote metadata for $GROUP/$SERVER_MODEL/$DATATYPE/$VAR"
  exit 0
fi

mapfile -t MATCHING_DATES < <(
  printf '%s\n' "${START_DATES[@]}" | grep -Fxf <(printf '%s\n' "${AVAIL_DATES[@]}") | sort -r
)

if [[ ${#MATCHING_DATES[@]} -eq 0 ]]; then
  echo "[DL] No matching dates in ${START_WINDOW}..${END_WINDOW} for $GROUP/$SERVER_MODEL/$VAR"
  exit 0
fi

for init_date in "${MATCHING_DATES[@]}"; do
  day="${init_date:6:2}"
  month_num="${init_date:4:2}"
  year="${init_date:0:4}"

  case "$month_num" in
    01) month="Jan" ;;
    02) month="Feb" ;;
    03) month="Mar" ;;
    04) month="Apr" ;;
    05) month="May" ;;
    06) month="Jun" ;;
    07) month="Jul" ;;
    08) month="Aug" ;;
    09) month="Sep" ;;
    10) month="Oct" ;;
    11) month="Nov" ;;
    12) month="Dec" ;;
    *)
      echo "[WARN] Invalid month in date token: $init_date"
      continue
      ;;
  esac

  s_part="S/%280000%20${day}%20${month}%20${year}%29VALUES"
  ingrid_url="${BASE_URL}/.${GROUP}/.${SERVER_MODEL}/.${DATATYPE}/.${VAR}/${s_part}/data.nc"
  out_file="${TARGET_DIR}/${VAR}_${GROUP}-${LOCAL_MODEL}_${init_date}.daily.nc"

  if [[ -f "$out_file" ]]; then
    echo "[DL] Exists, skipping: $out_file"
    continue
  fi

  echo "[DL] Downloading $ingrid_url -> $out_file"
  wget -q -c -O "$out_file" "$ingrid_url"
done

echo "[DL] Completed downloads for $GROUP/$SERVER_MODEL/$VAR"

