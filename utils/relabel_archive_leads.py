"""Relabel off-convention L (lead) coordinates in the local forecast archive, in place.

Consumes the manifest from ``audit_archive_leads.py``. Only the tiny ``L`` variable is
rewritten (data untouched). Every change is first appended to a revert manifest, and
each file is re-opened afterwards to verify L and a data checksum.

Dry-run by default. Usage:
  python3 utils/relabel_archive_leads.py --audit logs/lead_audit.jsonl --model ECCC-GEPS8 \
      [--var pr] [--date 20260608] [--apply] [--revert-manifest logs/lead_relabel_manifest.jsonl]
  python3 utils/relabel_archive_leads.py --revert logs/lead_relabel_manifest.jsonl --apply
"""
import argparse
import json
import os
import sys

import numpy as np

SHADOW_ROOT = "/data/esplab/subc-backup-direct-shadow"


def _checksum(path):
    """Cheap data fingerprint: sum of the first variable over a small block."""
    import netCDF4

    with netCDF4.Dataset(path) as ds:
        name = next(v for v in ds.variables if v not in ("S", "M", "L", "P", "Y", "X"))
        arr = ds[name]
        idx = tuple(slice(0, 1) if d in ("S", "M", "P") else slice(0, 3) for d in arr.dimensions)
        block = np.ma.filled(arr[idx].astype("float64"), np.nan)
        return float(np.nansum(block)), tuple(int(n) for n in arr.shape)


def _write_lead(path, values):
    import netCDF4

    with netCDF4.Dataset(path, "r+") as ds:
        ds["L"][:] = np.asarray(values, dtype=ds["L"].dtype)


def _read_lead(path):
    import netCDF4

    with netCDF4.Dataset(path) as ds:
        return np.asarray(ds["L"][:], dtype=np.float32)


def relabel_one(rec, apply, revert_path):
    path = rec["path"]
    if path.startswith(SHADOW_ROOT) or path.endswith(".tmp.nc"):
        return "skipped"
    if rec.get("status") != "relabel":
        return "skipped"
    old = np.asarray(rec["old"], dtype=np.float32)
    new = np.asarray(rec["new"], dtype=np.float32)
    if len(old) != len(new):
        return "skipped"
    if not apply:
        return "would_relabel"
    # The file must still have the audited labels (guards against concurrent ingest/rewrite).
    if not np.array_equal(_read_lead(path), old):
        return "changed_since_audit"
    before = _checksum(path)
    with open(revert_path, "a", encoding="utf-8") as m:
        m.write(json.dumps({"path": path, "old": rec["old"], "new": rec["new"]}) + "\n")
        m.flush()
        os.fsync(m.fileno())
    _write_lead(path, new)
    if not np.array_equal(_read_lead(path), new) or _checksum(path) != before:
        # Put the original labels back and report.
        _write_lead(path, old)
        return "verify_failed_reverted"
    return "relabeled"


def run(args):
    counts = {}
    with open(args.audit, encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            if rec.get("model") != args.model:
                continue
            if args.var and f"/{args.var}/" not in rec["path"]:
                continue
            if args.date and not rec["path"].endswith(f"_{args.date}.daily.nc"):
                continue
            res = relabel_one(rec, args.apply, args.revert_manifest)
            counts[res] = counts.get(res, 0) + 1
    print(f"[relabel] {'APPLY' if args.apply else 'DRY-RUN'} {args.model}: {counts}")
    return 0 if not {"changed_since_audit", "verify_failed_reverted"} & set(counts) else 1


def revert(args):
    n = 0
    with open(args.revert, encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            if args.apply:
                _write_lead(rec["path"], rec["old"])
            n += 1
    print(f"[relabel] {'REVERTED' if args.apply else 'would revert'} {n} files")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--audit", default="logs/lead_audit.jsonl")
    ap.add_argument("--model")
    ap.add_argument("--var")
    ap.add_argument("--date", help="Only files for this init date YYYYMMDD (pilot)")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--revert-manifest", default="logs/lead_relabel_manifest.jsonl")
    ap.add_argument("--revert", help="Revert from a manifest instead of relabeling")
    args = ap.parse_args()
    if args.revert:
        sys.exit(revert(args))
    if not args.model:
        ap.error("--model is required")
    os.makedirs(os.path.dirname(os.path.abspath(args.revert_manifest)), exist_ok=True)
    sys.exit(run(args))


if __name__ == "__main__":
    main()
