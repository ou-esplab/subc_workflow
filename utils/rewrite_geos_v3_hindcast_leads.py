"""Rewrite GEOS_V3 hindcast files so L is float32 0.5-based days (the SubX convention).

The retro-portal downloader stored ``L`` as int64 day offsets 0..N-1. NetCDF can't change a
variable's dtype in place, so each file is copied (every other variable bit-for-bit, masking
off), with only ``L`` replaced by ``float32(L + 0.5)`` plus ``units``/``long_name``/
``standard_name`` attributes. The copy is verified against the original before it atomically
replaces it. Files whose ``L`` is already float32 are skipped, so the tool is resumable.

Dry-run by default. Usage:
  python3 utils/rewrite_geos_v3_hindcast_leads.py [--var pr] [--date 19990101] [--workers 2] [--apply]
  python3 utils/rewrite_geos_v3_hindcast_leads.py --revert logs/geos_v3_lead_rewrite_manifest.jsonl --apply
"""
import argparse
import glob
import json
import os
import shutil
import sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np

DEFAULT_ROOT = "/data/esplab/subc-backup"
MODEL = "GMAO-GEOS_V3"
LEAD_ATTRS = {"units": "days", "long_name": "Lead", "standard_name": "forecast_period"}


def _forward(old):
    """int64 offsets 0..N-1 -> float32 0.5..N-0.5; None if the labels aren't the expected ones."""
    old = np.asarray(old)
    if old.dtype.kind not in "iu" or len(old) == 0 or old[0] != 0 or not np.all(np.diff(old) == 1):
        return None
    return (old.astype("float64") + 0.5).astype(np.float32)


def _backward(old):
    """float32 0.5..N-0.5 -> int64 0..N-1 (used by --revert); None if unexpected."""
    old = np.asarray(old)
    if old.dtype != np.float32 or len(old) == 0 or old[0] != np.float32(0.5) or not np.all(np.diff(old) == 1):
        return None
    return np.floor(old).astype("int64")


def _copy_with_new_lead(src, dst, new_lead, lead_attrs):
    import netCDF4

    with netCDF4.Dataset(src) as s, netCDF4.Dataset(dst, "w", format=s.data_model) as d:
        s.set_auto_maskandscale(False)
        d.set_auto_maskandscale(False)
        d.setncatts({k: s.getncattr(k) for k in s.ncattrs()})
        for name, dim in s.dimensions.items():
            d.createDimension(name, len(dim))
        for name, v in s.variables.items():
            attrs = {k: v.getncattr(k) for k in v.ncattrs()}
            fill = attrs.pop("_FillValue", None)
            if name == "L":
                out = d.createVariable("L", new_lead.dtype, v.dimensions)
                out.setncatts(lead_attrs)
                out[:] = new_lead
                continue
            flt = v.filters()
            out = d.createVariable(
                name, v.dtype, v.dimensions, fill_value=fill,
                zlib=bool(flt.get("zlib")), complevel=flt.get("complevel", 4) if flt.get("zlib") else 4,
                shuffle=bool(flt.get("shuffle")),
            )
            out.setncatts(attrs)
            if v.ndim >= 3:  # copy along the first dim in slices to bound memory
                for i in range(v.shape[0]):
                    out[i] = v[i]
            else:
                out[:] = v[:]


def _verify(src, dst, new_lead):
    import netCDF4

    with netCDF4.Dataset(src) as s, netCDF4.Dataset(dst) as d:
        s.set_auto_maskandscale(False)
        d.set_auto_maskandscale(False)
        if {k: len(v) for k, v in s.dimensions.items()} != {k: len(v) for k, v in d.dimensions.items()}:
            return "dimensions differ"
        if {k: s.getncattr(k) for k in s.ncattrs()} != {k: d.getncattr(k) for k in d.ncattrs()}:
            return "global attrs differ"
        if list(s.variables) != list(d.variables):
            return "variable list differs"
        for name, v in s.variables.items():
            w = d[name]
            if name == "L":
                if w.dtype != new_lead.dtype or not np.array_equal(w[:], new_lead):
                    return "L not as expected"
                continue
            if v.dtype != w.dtype or v.dimensions != w.dimensions:
                return f"{name}: dtype/dims differ"
            if {k: v.getncattr(k) for k in v.ncattrs()}.keys() != {k: w.getncattr(k) for k in w.ncattrs()}.keys():
                return f"{name}: attrs differ"
            if v.ndim >= 3:
                for i in range(v.shape[0]):
                    if not np.array_equal(v[i], w[i], equal_nan=v.dtype.kind == "f"):
                        return f"{name}: data differs (slice {i})"
            elif not np.array_equal(v[:], w[:], equal_nan=v.dtype.kind == "f"):
                return f"{name}: data differs"
    return None


def process(path, apply, reverse=False):
    """Return (status, record). Statuses: already_ok, unexpected, would_rewrite, rewritten, changed, verify_failed."""
    import netCDF4

    if path.endswith(".tmp.nc"):
        return "skipped", None
    with netCDF4.Dataset(path) as ds:
        old = np.asarray(ds["L"][:])
    if reverse:
        new, attrs = _backward(old), {}
    else:
        if old.dtype == np.float32:
            return "already_ok", None
        new, attrs = _forward(old), LEAD_ATTRS
    if new is None:
        return "unexpected", {"path": path, "dtype": str(old.dtype), "first": float(old[0]) if len(old) else None}
    rec = {"path": path, "n": int(len(old)), "old_first": float(old[0]), "new_first": float(new[0])}
    if not apply:
        return "would_rewrite", rec
    before = os.stat(path)
    tmp = path[: -len(".nc")] + ".tmp.nc"
    try:
        _copy_with_new_lead(path, tmp, new, attrs)
        problem = _verify(path, tmp, new)
        after = os.stat(path)
        if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
            problem = "file changed while rewriting"
        if problem:
            os.unlink(tmp)
            return ("changed" if "changed" in problem else "verify_failed"), {**rec, "problem": problem}
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except Exception as exc:
        if os.path.exists(tmp):
            os.unlink(tmp)
        return "verify_failed", {**rec, "problem": f"{type(exc).__name__}: {exc}"[:200]}
    return "rewritten", rec


def _work(args):
    path, apply, reverse = args
    return path, *process(path, apply, reverse)


def run(args):
    if args.revert:
        with open(args.revert, encoding="utf-8") as f:
            paths = [json.loads(line)["path"] for line in f]
        reverse = True
    else:
        date_glob = args.date or "????????"
        paths = sorted(glob.glob(f"{args.root}/{MODEL}/hindcast/{args.var or '*'}/*_{MODEL}_{date_glob}.daily.nc"))
        reverse = False
    paths = [p for p in paths if not p.endswith(".tmp.nc")]
    print(f"[geos_v3_lead_rewrite] {'APPLY' if args.apply else 'DRY-RUN'}{' (revert)' if reverse else ''}: {len(paths)} files", flush=True)
    counts = {}
    os.makedirs(os.path.dirname(os.path.abspath(args.manifest)), exist_ok=True)
    jobs = [(p, args.apply, reverse) for p in paths]
    with ProcessPoolExecutor(max_workers=max(1, args.workers)) as pool, \
            open(args.manifest if not reverse else os.devnull, "a", encoding="utf-8") as man:
        for i, (path, status, rec) in enumerate(pool.map(_work, jobs, chunksize=4), 1):
            counts[status] = counts.get(status, 0) + 1
            if status == "rewritten" and not reverse:
                man.write(json.dumps(rec) + "\n")
                man.flush()
                os.fsync(man.fileno())
            elif status in ("unexpected", "verify_failed", "changed"):
                print(f"[geos_v3_lead_rewrite] {status}: {rec}", flush=True)
            if i % 200 == 0:
                print(f"[geos_v3_lead_rewrite] {i}/{len(paths)} {counts}", flush=True)
    print(f"[geos_v3_lead_rewrite] done: {counts}", flush=True)
    return 1 if {"unexpected", "verify_failed", "changed"} & set(counts) else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--var")
    ap.add_argument("--date", help="Only this init date YYYYMMDD (pilot)")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--manifest", default="logs/geos_v3_lead_rewrite_manifest.jsonl")
    ap.add_argument("--revert", help="Revert the files listed in this manifest to int64 0..N-1")
    sys.exit(run(ap.parse_args()))


if __name__ == "__main__":
    main()
