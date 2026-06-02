#!/usr/bin/env bash
set -euo pipefail

# Self-test for shadow direct ingest + parity validation.
#
# What this does:
# 1) Runs ingest/validation unit tests
# 2) Runs ingest in stub mode (no remote downloads)
# 3) Runs preprocess validation
# 4) Prints parity summary from shadow_parity_manifest.json
#
# Usage:
#   scripts/selftest_shadow_ingest.sh [INIT_YYYYMMDD] [CONFIG_PATH]
#
# Defaults:
#   INIT_YYYYMMDD: 20260305
#   CONFIG_PATH:   config.yaml

INIT_DATE="${1:-20260305}"
CONFIG_IN="${2:-config.yaml}"
CONFIG="$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$CONFIG_IN")"

if [[ ! -f "$CONFIG" ]]; then
  echo "[FATAL] Config not found: $CONFIG_IN -> $CONFIG" >&2
  exit 2
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "==> [selftest] init=$INIT_DATE config=$CONFIG"
echo "==> [selftest] Step 1/4: unit tests"
python3 -m unittest tests/test_download_script.py tests/test_validate_realtime.py -v

echo "==> [selftest] Step 2/4: ingest stage (stub mode)"
SUBX_DOWNLOAD_STUB=1 python3 runners/cli.py --system subx --config "$CONFIG" --init "$INIT_DATE" --stages ingest

echo "==> [selftest] Step 3/4: preprocess stage (parity manifest)"
python3 runners/cli.py --system subx --config "$CONFIG" --init "$INIT_DATE" --stages preprocess

echo "==> [selftest] Step 4/4: locate and summarize parity manifest"
OUT_WEEKLY="$(python3 - "$CONFIG" <<'PY'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1])) or {}
print((cfg.get("paths") or {}).get("out_weekly", ""))
PY
)"

if [[ -z "$OUT_WEEKLY" ]]; then
  echo "[WARN] paths.out_weekly not set; cannot locate manifests automatically."
  exit 0
fi

MANIFEST="$OUT_WEEKLY/$INIT_DATE/data/shadow_parity_manifest.json"
if [[ ! -f "$MANIFEST" ]]; then
  echo "[WARN] No shadow parity manifest found at: $MANIFEST"
  echo "       Ensure ingest.shadow.enabled=true and ingest.shadow.model_source is configured."
  exit 0
fi

python3 - "$MANIFEST" <<'PY'
import json, sys
path = sys.argv[1]
obj = json.load(open(path))
s = obj.get("summary", {})
print("[selftest][PARITY-SUMMARY] total={total} presence_ok={presence_ok} presence_missing={presence_missing} schema_ok={schema_ok} schema_mismatch={schema_mismatch} schema_unavailable={schema_unavailable}".format(
    total=s.get("total", 0),
    presence_ok=s.get("presence_ok", 0),
    presence_missing=s.get("presence_missing", 0),
    schema_ok=s.get("schema_ok", 0),
    schema_mismatch=s.get("schema_mismatch", 0),
    schema_unavailable=s.get("schema_unavailable", 0),
))
print("[selftest] manifest:", path)
PY

echo "==> [selftest] completed"
