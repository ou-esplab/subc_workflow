"""
One-time repair for the L (lead, days) coordinate in ou-subc/subc-forecasts.

Earthmover reported (2026-10) that 5 of the 6 forecast groups store L as
int64 / units "nanoseconds" / dtype "timedelta64[ns]" with integer values --
a legacy group-creation write that lost the half-day offsets (GMAO/RSMAS/FIM
collapsed to 0,2,2,4,4,... by round-half-even). NCEP-CFSv2 is float32 days
but labelled 1..44 instead of the IRIDL grid 0.5..43.5.

L is only written when a group is created (appends never touch it), so this is
a coordinate-only repair: the data arrays are untouched. For each group, L is
recreated as float32 days with the authoritative IRIDL grid below and the
attributes of the matching ou-subc/subc-hindcasts L (the right encoding to copy).

Usage: python3 arraylake/repair_lead_coordinate.py --config config.yaml [--dry-run] [--groups ...]
"""
import argparse
import os
import sys
import traceback

import numpy as np
import xarray as xr
import yaml
import zarr
from arraylake import Client

# Authoritative lead grids (days) from IRIDL: (first, last, N).
IRIDL_L = {
    "eccc-geps8-forecast": (0.5, 38.5, 39),
    "emc-gefsv12_cpc-forecast": (1.5, 34.5, 34),
    "esrl-fimr1p1-forecast": (0.5, 31.5, 32),
    "gmao-geos_v2p1_5daily-forecast": (0.5, 44.5, 45),
    "rsmas-ccsm4-forecast": (0.5, 44.5, 45),
    "ncep-cfsv2-forecast": (0.5, 43.5, 44),
}

HINDCAST_REPO = "ou-subc/subc-hindcasts"
DROP_ATTRS = {"dtype"}


def load_token(script_dir):
    tok = os.environ.get("ARRAYLAKE_TOKEN")
    if tok:
        return tok
    env_file = os.path.join(script_dir, ".env")
    if os.path.exists(env_file):
        with open(env_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("ARRAYLAKE_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("ARRAYLAKE_TOKEN not found in environment or arraylake/.env")


def target_lead(group_name):
    first, last, n = IRIDL_L[group_name]
    values = np.linspace(first, last, n).astype(np.float32)
    assert np.allclose(np.diff(values), 1.0), group_name
    return values


def template_attrs(hind_root, group_name, current_attrs):
    """Attributes for the new L: the hindcast group's L attrs, else the current ones."""
    hind_group = group_name.replace("-forecast", "-hindcast")
    attrs = None
    if hind_root is not None and hind_group in hind_root:
        attrs = dict(hind_root[hind_group]["L"].attrs)
    if attrs is None:
        attrs = {k: v for k, v in current_attrs.items() if k not in DROP_ATTRS and k != "_FillValue"}
    attrs = {k: v for k, v in attrs.items() if k not in DROP_ATTRS}
    attrs["units"] = "days"
    return attrs


def repair_group(repo, branch_name, hind_root, group_name, dry_run):
    print("=" * 70)
    print(f"Repairing L in {group_name}")

    zg = zarr.open_group(repo.readonly_session(branch_name).store, path=group_name, mode="r")
    l_arr = zg["L"]
    old = l_arr[:]
    new = target_lead(group_name)
    print(f"  Current : dtype={l_arr.dtype} units={l_arr.attrs.get('units')!r} "
          f"first/last={old[0]}/{old[-1]} N={len(old)}")
    print(f"  Target  : dtype=float32 units='days' first/last={new[0]}/{new[-1]} N={len(new)}")

    if len(old) != len(new):
        print(f"  ✗ L length {len(old)} != expected {len(new)}; aborting (data arrays would not line up)")
        return False
    for v in zg.array_keys():
        arr = zg[v]
        if arr.ndim >= 5 and arr.shape[arr.metadata.dimension_names.index("L")] != len(new):
            print(f"  ✗ {v} L-axis length does not match; aborting")
            return False
    if l_arr.dtype == np.float32 and np.array_equal(old, new) and l_arr.attrs.get("units") == "days" \
            and "dtype" not in l_arr.attrs:
        print("  ℹ Already correct; nothing to do")
        return True

    attrs = template_attrs(hind_root, group_name, dict(l_arr.attrs))
    print(f"  New attrs: {attrs}")

    if dry_run:
        print("  [DRY RUN] Would delete and recreate L (no writes made)")
        return True

    # Spot-check one data pixel before and after to prove data is untouched.
    data_var = next(v for v in zg.array_keys() if zg[v].ndim >= 5)
    sel = (0,) * (zg[data_var].ndim - 2) + (slice(None, 3), slice(None, 3))
    before = np.asarray(zg[data_var][sel])

    write_session = repo.writable_session(branch_name)
    wzg = zarr.open_group(write_session.store, path=group_name, mode="r+")
    del wzg["L"]
    new_arr = wzg.create_array(
        "L", shape=new.shape, chunks=new.shape, dtype="float32",
        dimension_names=("L",), fill_value=float("nan"), attributes=attrs,
    )
    new_arr[:] = new

    try:
        staged = xr.open_zarr(write_session.store, group=group_name, consolidated=False)
        L = staged["L"]
        if L.dtype != np.float32 or L.attrs.get("units") != "days" and L.encoding.get("units") != "days":
            raise RuntimeError(f"Unexpected L dtype/units after rewrite: {L.dtype} {L.attrs}")
        if not np.array_equal(L.values, new) or not L.to_index().is_monotonic_increasing or not L.to_index().is_unique:
            raise RuntimeError("L values not as expected / not monotonic-unique")
        after = np.asarray(zarr.open_group(write_session.store, path=group_name, mode="r")[data_var][sel])
        if not np.array_equal(before, after, equal_nan=True):
            raise RuntimeError(f"Data spot-check for {data_var} changed after L rewrite")
        print(f"  ✓ Verified: L float32 days {L.values[0]}..{L.values[-1]}, {data_var} spot-check unchanged")
    except Exception as e:
        print(f"  ✗ Verification failed, not committing: {e}")
        print(traceback.format_exc())
        return False

    msg = f"Fix L coordinate: float32 days {new[0]}..{new[-1]} (was {old[0]}..{old[-1]} {l_arr.dtype})"
    write_session.commit(msg)
    print(f"  ✓ Committed: {msg}")
    return True


def main():
    parser = argparse.ArgumentParser(description="One-time repair of the L coordinate in subc-forecasts")
    parser.add_argument("--config", required=True, help="Path to workflow config.yaml")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--groups", nargs="*", default=list(IRIDL_L.keys()), help="Subset of groups to repair")
    args = parser.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    al_cfg = cfg.get("arraylake") or {}
    branch_name = al_cfg.get("branch", "main")
    org_name = al_cfg.get("repo", "ou-subc/subc-forecasts")

    client = Client(token=load_token(os.path.dirname(os.path.abspath(__file__))))
    repo = client.get_repo(org_name)
    try:
        hind_root = zarr.open_group(client.get_repo(HINDCAST_REPO).readonly_session("main").store, mode="r")
    except Exception as e:
        print(f"⚠ Could not read hindcast repo for attribute template ({e}); using current attrs")
        hind_root = None

    results = {}
    for group_name in args.groups:
        try:
            results[group_name] = repair_group(repo, branch_name, hind_root, group_name, args.dry_run)
        except Exception as e:
            print(f"✗ Unexpected error repairing {group_name}: {e}")
            print(traceback.format_exc())
            results[group_name] = False

    print("=" * 70)
    print("REPAIR SUMMARY")
    for g, ok in results.items():
        print(f"  {'✓' if ok else '✗'} {g}")
    if not all(results.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
