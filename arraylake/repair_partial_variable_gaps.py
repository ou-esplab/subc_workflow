"""
One-time repair for arraylake groups whose per-variable S (start-date) arrays
fell out of sync -- some variables missing their most recent start-date slot --
due to a since-fixed bug in update_arraylake_fcsts.py's append logic (partial
variable coverage used to be appended for only some variables, desyncing the
group's per-variable S lengths and breaking every future xr.open_zarr read).

For each known-affected group, this script:
  1. Opens the group via raw zarr access (xr.open_zarr can't open a desynced
     group at all, which is exactly the problem being fixed).
  2. Confirms every lagging variable is short by exactly one trailing S slot
     (aborts that group, untouched, if the state doesn't match -- the expected
     shapes below are a sanity check, not a blind assumption).
  3. Resizes each lagging variable's array to match the group's S length and
     NaN-fills the new slot.
  4. Checks whether real source data is now available locally on disk for the
     gap date; if so, overwrites the NaN slot with real data via a raw zarr
     region write (same technique as update_arraylake_fcsts.py's Section 4
     backfill check).
  5. Reopens the group with xr.open_zarr to verify it's structurally sound
     before committing.

Usage: python3 arraylake/repair_partial_variable_gaps.py --config config.yaml [--dry-run]
"""
import argparse
import os
import sys
import traceback

import numpy as np
import pandas as pd
import xarray as xr
import yaml
import zarr
from arraylake import Client

# Sanity-check expectations from the 2026-07-03 investigation (see the approved
# repair plan). The script always reads the group's live state and aborts that
# group (leaving it untouched) if reality doesn't match -- these are guardrails,
# not assumptions the writes rely on.
EXPECTED = {
    "emc-gefsv12_cpc-forecast": {"lagging_vars": {"pr", "tas"}, "gap_date": "20260625"},
    "gmao-geos_v2p1_5daily-forecast": {"lagging_vars": {"tas", "ua"}, "gap_date": "20260625"},
    "rsmas-ccsm4-forecast": {"lagging_vars": {"rlut", "ts", "va"}, "gap_date": "20260621"},
}

NON_DATA_ARRAYS = {"S", "M", "L", "P", "Y", "X"}


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


def decode_s_value(raw_value, units, calendar):
    # units look like "days since 2022-04-02 00:00:00"; calendar is
    # proleptic_gregorian throughout this repo, which matches pandas' own
    # calendar for the date ranges involved here.
    base = pd.Timestamp(units.split("since", 1)[1].strip())
    return base + pd.Timedelta(days=int(raw_value))


def find_model_cfg(cfg, group_name):
    model_name_map = cfg.get("model_name_map") or {}
    for model in cfg.get("models") or []:
        group, name = model.get("group"), model.get("name")
        if not group or not name:
            continue
        candidate = f"{group.lower()}-{name.lower()}-forecast"
        if candidate == group_name:
            local_model = model_name_map.get(f"{group}-{name}", name)
            return group, name, local_model, model.get("vars") or []
    return None


def repair_group(repo, branch_name, group_name, input_path, cfg, dry_run):
    print("=" * 70)
    print(f"Repairing {group_name}")

    model_cfg = find_model_cfg(cfg, group_name)
    if model_cfg is None:
        print(f"  ✗ No matching model in config.yaml for {group_name}; skipping")
        return False
    this_group, this_model, this_local_model, configured_vars = model_cfg

    read_store = repo.readonly_session(branch_name).store
    zg = zarr.open_group(read_store, path=group_name, mode="r")

    s_arr = zg["S"]
    master_len = s_arr.shape[0]
    s_units = s_arr.attrs["units"]
    s_calendar = s_arr.attrs.get("calendar", "proleptic_gregorian")
    gap_date = decode_s_value(s_arr[master_len - 1], s_units, s_calendar)
    gap_date_str = gap_date.strftime("%Y%m%d")
    print(f"  Master S length: {master_len}; last date: {gap_date_str}")

    lagging_vars = [
        v for v in configured_vars
        if v in zg.array_keys() and v not in NON_DATA_ARRAYS and zg[v].shape[0] < master_len
    ]
    print(f"  Lagging variables: {lagging_vars}")

    expected = EXPECTED.get(group_name)
    if expected is not None:
        if set(lagging_vars) != expected["lagging_vars"] or gap_date_str != expected["gap_date"]:
            print(
                f"  ✗ State does not match expected ({expected}); "
                f"aborting repair for this group to avoid acting on drifted state."
            )
            return False

    for v in lagging_vars:
        gap = master_len - zg[v].shape[0]
        if gap != 1:
            print(f"  ✗ {v} is short by {gap} slots (expected exactly 1); aborting repair for this group")
            return False

    if not lagging_vars:
        print("  ℹ No lagging variables found; nothing to repair")
        return True

    if dry_run:
        print(f"  [DRY RUN] Would resize+NaN-fill {lagging_vars} to length {master_len}, "
              f"then check for local real data for {gap_date_str}")
        return True

    write_session = repo.writable_session(branch_name)
    write_store = write_session.store
    commit_messages = []

    p_values = zg["P"][:] if "P" in zg.array_keys() else None

    for v in lagging_vars:
        arr = zarr.open_array(write_store, path=f"{group_name}/{v}")
        old_len = arr.shape[0]
        new_shape = (master_len,) + arr.shape[1:]
        arr.resize(new_shape)
        arr[old_len:master_len, ...] = np.nan
        print(f"  ✓ Resized {v} from {old_len} to {master_len} (NaN-filled new slot)")
        commit_messages.append(f"Resized {v} to {master_len} (NaN-filled slot for {gap_date_str})")

        has_p = arr.ndim == 6
        f = (f"{input_path}/{this_group}-{this_local_model}/forecast/{v}/"
             f"{v}_{this_group}-{this_local_model}_{gap_date_str}.daily.nc")
        if not os.path.exists(f) or os.path.getsize(f) == 0:
            print(f"    ℹ No local source file for {v} {gap_date_str}; leaving NaN")
            continue

        try:
            src_ds = xr.open_dataset(f, engine="netcdf4", chunks={})
            src_da = src_ds[v]
            if "P" in src_da.dims:
                valid = ~src_da.isel(S=0, M=0, L=0, P=0).isnull().all(dim=("Y", "X"))
            else:
                valid = ~src_da.isel(S=0, M=0, L=0).isnull().all(dim=("Y", "X"))
            if not bool(valid.compute()):
                print(f"    ℹ Local source file for {v} {gap_date_str} is all-NaN; leaving NaN")
                continue

            dim_order = ("S", "M", "L", "P", "Y", "X") if has_p else ("S", "M", "L", "Y", "X")
            src_da = src_da.transpose(*[d for d in dim_order if d in src_da.dims])
            if has_p and p_values is not None:
                src_da = src_da.reindex(P=p_values)
            src_da = src_da.isel(S=0).astype(arr.dtype)
            values = src_da.values

            arr[old_len:master_len, ...] = values[np.newaxis, ...]
            print(f"    ✓ Backfilled {v} {gap_date_str} with real data")
            commit_messages.append(f"Backfilled {v} for {gap_date_str} with real data")
        except Exception as e:
            print(f"    ⚠ Failed to backfill {v} {gap_date_str} with real data, leaving NaN: {e}")
            print(traceback.format_exc())

    try:
        staged_ds = xr.open_zarr(write_store, group=group_name, consolidated=False)
        new_lens = {v: staged_ds[v].sizes["S"] for v in staged_ds.data_vars}
        if any(n != master_len for n in new_lens.values()):
            raise RuntimeError(f"Post-repair S lengths still inconsistent: {new_lens}")
        if gap_date not in pd.Index(staged_ds["S"].values):
            raise RuntimeError(f"Gap date {gap_date_str} missing from repaired S coordinate")
        print(f"  ✓ Verified: all variables at S length {master_len}, {gap_date_str} present")
    except Exception as e:
        print(f"  ✗ Verification failed, not committing: {e}")
        print(traceback.format_exc())
        return False

    write_session.commit("; ".join(commit_messages))
    print(f"  ✓ Committed: {'; '.join(commit_messages)}")
    return True


def main():
    parser = argparse.ArgumentParser(description="One-time repair for partial-variable-gap arraylake groups")
    parser.add_argument("--config", required=True, help="Path to workflow config.yaml")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--groups", nargs="*", default=list(EXPECTED.keys()), help="Subset of groups to repair")
    args = parser.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    al_cfg = cfg.get("arraylake") or {}
    input_path = str(al_cfg.get("input_root") or (cfg.get("paths") or {}).get("rt_root")).rstrip("/")
    branch_name = al_cfg.get("branch", "main")
    org_name = al_cfg.get("repo", "ou-subc/subc-forecasts")

    script_dir = os.path.dirname(os.path.abspath(__file__))
    client = Client(token=load_token(script_dir))
    repo = client.get_repo(org_name)

    results = {}
    for group_name in args.groups:
        try:
            results[group_name] = repair_group(repo, branch_name, group_name, input_path, cfg, args.dry_run)
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
