"""Read-only audit of the L (lead) labels in the local forecast/hindcast archive.

Reads only the small ``L`` variable of every ``<root>/<model>/<kind>/<var>/*.daily.nc``
and writes a JSONL manifest of files whose lead labels differ from the canonical
SubX labels (see ``download_subx_direct._SUBX_FIRST_LEAD_DAYS``). The relabel tool
(``relabel_archive_leads.py``) consumes this manifest.

Canonical L is float32 days. Forecasts use the provider's first lead
(``_SUBX_FIRST_LEAD_DAYS``; GEFS forecast starts at 1.5 on purpose). Hindcasts start at
0.5 for every model. Models without a fixed length (GEOS_V2p1, CCSM4, GEOS_V3, and all
hindcasts) are checked as ``first + arange(len(L))``. A non-float32 ``L`` (GEOS_V3
hindcast stores int64 offsets) is reported as ``dtype_mismatch`` and never relabeled in
place -- it needs a file rewrite (``rewrite_geos_v3_hindcast_leads.py``).

Usage: python3 utils/audit_archive_leads.py --model ECCC-GEPS8 [--kind hindcast] [--var pr] \
           [--out logs/lead_audit.jsonl]
"""
import argparse
import glob
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from functools import partial

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from download_subx_direct import _subx_lead_days  # noqa: E402

DEFAULT_ROOT = "/data/esplab/subc-backup"

# archive model dir -> (_SUBX_FIRST_LEAD_DAYS provider key, expected number of leads)
CANONICAL = {
    "ECCC-GEPS8": ("ECCC", 39),
    "NCEP-CFSv2": ("CFS", 44),
    "ESRL-FIMr1p1": ("FIM", 32),
    "EMC-GEFSv12_CPC": ("GEFS", 34),
}


# Models audited with a free length (first lead 0.5, length taken from the file).
FREE_LENGTH_MODELS = ["GMAO-GEOS_V2p1_5daily", "GMAO-GEOS_V3", "RSMAS-CCSM4"]
KINDS = ("forecast", "hindcast")
HINDCAST_FIRST_LEAD = 0.5


def canonical_lead(model, kind="forecast", n_have=None):
    """Canonical L for ``model``; ``None`` when the length is free and ``n_have`` is unknown."""
    if kind == "forecast" and model in CANONICAL:
        provider, n = CANONICAL[model]
        return _subx_lead_days(provider, n)
    if n_have is None:
        return None
    first = HINDCAST_FIRST_LEAD if kind == "hindcast" else 0.5
    return (first + np.arange(n_have)).astype(np.float32)


def read_lead(path):
    import netCDF4

    with netCDF4.Dataset(path) as ds:
        return np.asarray(ds["L"][:], dtype=np.float32)


def _read_lead_raw(path):
    """Return (L values as float32, original dtype string)."""
    import netCDF4

    with netCDF4.Dataset(path) as ds:
        var = ds["L"]
        return np.asarray(var[:], dtype=np.float32), str(var.dtype)


def audit_file(path, model, kind="forecast"):
    """Return a manifest record if the file's L is off-convention, else None."""
    try:
        have, dtype = _read_lead_raw(path)
    except Exception as exc:  # unreadable file: report, never touch
        return {"path": path, "model": model, "kind": kind, "status": "unreadable", "error": str(exc)[:200]}
    want = canonical_lead(model, kind, n_have=len(have))
    if len(have) != len(want):
        return {"path": path, "model": model, "kind": kind, "status": "length_mismatch",
                "n_have": int(len(have)), "n_want": int(len(want))}
    if dtype != "float32":
        return {"path": path, "model": model, "kind": kind, "status": "dtype_mismatch",
                "dtype": dtype, "old": [float(x) for x in have], "new": [float(x) for x in want]}
    if np.array_equal(have, want):
        return None
    return {"path": path, "model": model, "kind": kind, "status": "relabel",
            "old": [float(x) for x in have], "new": [float(x) for x in want]}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", required=True, choices=sorted(set(CANONICAL) | set(FREE_LENGTH_MODELS)))
    ap.add_argument("--kind", default="forecast", choices=KINDS)
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--var", default=None, help="Limit to one variable directory")
    ap.add_argument("--out", default="logs/lead_audit.jsonl")
    ap.add_argument("--workers", type=int, default=1,
                    help="Parallel readers; the audit is NFS-latency bound, so >1 scales well")
    args = ap.parse_args()

    var_glob = args.var or "*"
    files = sorted(
        f for f in glob.glob(f"{args.root}/{args.model}/{args.kind}/{var_glob}/*.daily.nc")
        if not f.endswith(".tmp.nc")
    )
    print(f"[audit] {args.model} {args.kind}: {len(files)} files")
    counts = {"ok": 0, "relabel": 0, "dtype_mismatch": 0, "length_mismatch": 0, "unreadable": 0}
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "a", encoding="utf-8") as out, ProcessPoolExecutor(max_workers=max(1, args.workers)) as pool:
        recs = pool.map(partial(audit_file, model=args.model, kind=args.kind), files, chunksize=50)
        for i, rec in enumerate(recs, 1):
            if rec is None:
                counts["ok"] += 1
            else:
                counts[rec["status"]] += 1
                out.write(json.dumps(rec) + "\n")
            if i % 5000 == 0:
                print(f"[audit] {i}/{len(files)} {counts}", flush=True)
    print(f"[audit] done {args.model} {args.kind}: {counts}")


if __name__ == "__main__":
    main()
