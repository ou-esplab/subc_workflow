#!/usr/bin/env python3
"""Download GMAO GEOS-S2S-3 retrospective (hindcast) t2m/precip data.

GMAO has processed t2m and precip for all GEOS-S2S-3 retrospective forecasts
and published them on the NASA HECC data portal under SubC/. Each
subdirectory is named by init date (YYYYMMDD); inside it holds one directory
per ensemble member (ens1-ens5 normally, ens1-ens15 on the last init date of
each month, where ens6-15 are ocean-perturbed members). Each member directory
holds one file per lead day (~90 lead days), bundling both variables:

  SubC/
    20260101/
      ens1/
        20260101.sfc_tavg_3hr_glo_L720x361_sfc.dailymean.20260102.t2mprectot.nc4
        20260101.sfc_tavg_3hr_glo_L720x361_sfc.dailymean.20260103.t2mprectot.nc4
        ...
      ens2/
      ...

Each file has data variables T2M (K) and PRECTOTCORR (kg m-2 s-1) on a native
720x361 0.5-degree grid (lon -180..179.5). Both units already match this
archive's convention, so only variable renaming is needed (T2M -> tas,
PRECTOTCORR -> pr). The native grid is an exact 2:1 superset of the target
360x181 1-degree (0-360 lon) grid used elsewhere in this archive, so output
is produced by subsampling rather than interpolating.

Output files match the existing hindcast convention:
  {rt_root}/GMAO-GEOS_V3/hindcast/{var}/{var}_GMAO-GEOS_V3_{YYYYMMDD}.daily.nc
  dims: (S: 1, M: 5-or-15, L: N, Y: 181, X: 360)

Both variables are fetched in the same pass per member (they're bundled in
one file per lead day), so a single run produces both pr and tas outputs.

Usage
-----
  # Dry-run: preview what would be downloaded (fast, no data transferred)
  python static/download_geos_v3_hindcast.py --config config.yaml --dry-run --max-dates 1

  # Download everything found on the server
  python static/download_geos_v3_hindcast.py --config config.yaml

  # Limit to a date range (inclusive, YYYYMMDD)
  python static/download_geos_v3_hindcast.py --config config.yaml \\
      --start-date 19990101 --end-date 19990101

  # Overwrite existing files
  python static/download_geos_v3_hindcast.py --config config.yaml --overwrite
"""
from __future__ import annotations

import argparse
import re
import sys
import tempfile
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin
from urllib.request import urlopen, urlretrieve

import numpy as np
import pandas as pd
import xarray as xr
import yaml

warnings.filterwarnings("ignore")


BASE_URL = "https://data.nas.nasa.gov/geos-s2s-3/SubC/"
MODEL_ID = "GMAO-GEOS_V3"
VARS = ["pr", "tas"]
_VAR_RENAME = {"PRECTOTCORR": "pr", "T2M": "tas"}


# ── HTTP listing helpers ──────────────────────────────────────────────────────

def _http_get(url: str, timeout: int = 60, retries: int = 4, sleep_secs: float = 5.0) -> str:
    last_exc: Optional[Exception] = None
    for attempt in range(retries):
        try:
            with urlopen(url, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="ignore")
        except Exception as exc:
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(sleep_secs * (2 ** attempt))
    raise RuntimeError(f"Failed to fetch {url} after {retries} attempts") from last_exc


def _list_init_dates(base_url: str) -> List[str]:
    """Return sorted list of YYYYMMDD init dates on the server."""
    text = _http_get(base_url)
    return sorted(set(re.findall(r'href=["\'][^"\']*/(\d{8})/["\']', text)))


def _list_members(date_url: str) -> List[Tuple[int, str]]:
    """Return sorted list of (member_index, member_url) for one init date dir."""
    text = _http_get(date_url)
    names = set(re.findall(r'href=["\'][^"\']*/(ens\d+)/["\']', text))
    members = []
    for name in names:
        m = re.match(r"ens(\d+)$", name)
        if m:
            members.append((int(m.group(1)), urljoin(date_url, f"{name}/")))
    return sorted(members)


def _list_lead_files(member_url: str) -> List[Tuple[str, str]]:
    """Return sorted list of (lead_date YYYYMMDD, file_url) for one member dir.

    Most init dates publish a pre-extracted t2m/precip-only bundle
    (...dailymean.{lead}.t2mprectot.nc4, ~2MB). A handful of later dates
    (seen starting 2012-12) only have the full raw diagnostic dump instead
    (...dailymean.{lead}.nc4, ~230MB, ~150 variables including T2M and
    PRECTOTCORR among many unneeded ones) -- both are matched here since
    _load_lead_day only ever reads out the two variables it needs.

    Note: a handful of members on a few dates (e.g. 2003-02-xx ens2-ens15)
    have server-side lead files extending 300+ days past init -- confirmed
    2026-07-13 to be real, not a caching/corruption artifact. This archive
    intentionally keeps the raw hindcast files at full fidelity as fetched;
    any lead-time cap for downstream products (climatology/percentiles) is
    applied there instead of at ingest, so it stays a one-line, easily
    revisited choice rather than baked into what's on disk.
    """
    text = _http_get(member_url)
    names = re.findall(r'href=["\']([^"\']*dailymean\.\d{8}(?:\.t2mprectot)?\.nc4)["\']', text)
    entries: List[Tuple[str, str]] = []
    for name in names:
        bare = name.split("/")[-1]
        m = re.search(r"dailymean\.(\d{8})(?:\.t2mprectot)?\.nc4$", bare)
        if not m:
            continue
        entries.append((m.group(1), urljoin(member_url, bare)))
    return sorted(set(entries))


# ── Download helpers ──────────────────────────────────────────────────────────

def _download_with_retry(
    url: str, dest: Path, retries: int = 3, sleep_secs: float = 3.0
) -> bool:
    """Download url to dest, with exponential-backoff retries."""
    for attempt in range(retries):
        try:
            urlretrieve(url, str(dest))
            return True
        except Exception as exc:
            if dest.exists():
                dest.unlink(missing_ok=True)
            wait = sleep_secs * (2 ** attempt)
            if attempt < retries - 1:
                time.sleep(wait)
            else:
                print(f"      [ERROR] Giving up on {url}: {exc}")
    return False


# ── NetCDF conversion ─────────────────────────────────────────────────────────

def _regrid_to_1deg(ds: xr.Dataset) -> xr.Dataset:
    """Subsample native 720x361 0.5-deg (-180..179.5) grid to 360x181 1-deg (0..359).

    The native grid is an exact 2:1 superset of the target grid, so no
    interpolation is needed. Longitude is snapped to the nearest 0.5 before
    the 0-360 conversion, otherwise floating-point noise at lon~=0 (observed
    as -1.3e-12 rather than exactly 0.0) wraps that point to ~359.999 and
    shifts every subsequent sample by one index.
    """
    lon_snapped = np.round(ds["lon"].values * 2.0) / 2.0
    lon_0_360 = np.mod(lon_snapped, 360.0)
    ds = ds.assign_coords(lon=lon_0_360).sortby("lon")
    ds = ds.isel(lat=slice(0, None, 2), lon=slice(0, None, 2))

    expected_lat = np.arange(-90, 91, 1, dtype=float)
    expected_lon = np.arange(0, 360, 1, dtype=float)
    if not (
        np.allclose(ds["lat"].values, expected_lat)
        and np.allclose(ds["lon"].values, expected_lon)
    ):
        raise RuntimeError(
            f"Regrid did not produce the expected 181x360 grid "
            f"(got lat={ds['lat'].values[:3]}..{ds['lat'].values[-3:]}, "
            f"lon={ds['lon'].values[:3]}..{ds['lon'].values[-3:]})"
        )
    # allclose above only validates closeness -- the subsampled native grid's
    # floating-point values (e.g. the equator landing at ~2.9e-13 instead of
    # exactly 0.0) were never actually replaced. Assign the exact values so
    # this matches other models' lat/lon bit-for-bit; otherwise downstream
    # multi-model merges treat the near-0.0 as a distinct coordinate from
    # every other model's exact 0.0, inflating the merged grid.
    ds = ds.assign_coords(lat=expected_lat, lon=expected_lon)
    return ds


def _load_lead_day(nc_path: Path, init_date: str, lead_date: str) -> Optional[Dict[str, xr.DataArray]]:
    """Open one lead-day file, regrid, and return {var: (Y,X) DataArray}."""
    ds = xr.open_dataset(nc_path)
    missing = [v for v in _VAR_RENAME if v not in ds]
    if missing:
        print(f"      [WARN] {nc_path.name} missing {missing}; data_vars={list(ds.data_vars)}")
        ds.close()
        return None

    ds = _regrid_to_1deg(ds)
    ds = ds.rename({k: v for k, v in _VAR_RENAME.items() if k in ds})
    ds = ds.rename({"lat": "Y", "lon": "X"})

    lead_offset = (pd.Timestamp(lead_date) - pd.Timestamp(init_date)).days
    result = {}
    for var in VARS:
        da = ds[var].isel(time=0, drop=True).assign_coords(L=lead_offset)
        result[var] = da.astype(np.float32)
    ds.close()
    return result


# ── Per-member / per-date processing ──────────────────────────────────────────

def _download_members_flat(
    members: List[Tuple[int, str]], tmpdir: Path, workers: int
) -> Dict[int, Dict[str, Path]]:
    """Download lead-day files for MULTIPLE members through one shared pool.

    Previously each member was downloaded via its own ThreadPoolExecutor in a
    plain `for member in members` loop, with an explicit sleep between them --
    so members were fully serialized even though `workers` files could
    download concurrently within any one member. For a 5-member date that's 5x
    the wall-clock time of downloading one member's ~60-92 lead-day files, for
    no benefit (confirmed 2026-08-10: retrying 107 partial dates was tracking
    the exact per-date rate the original 856-date backfill took, ~3 min/date).
    Flattening every (member, lead_date) pair across ALL requested members into
    a single pool lets `workers` downloads run concurrently across members too,
    not just within one.
    """
    all_tasks: List[Tuple[int, str, str]] = []
    for member_idx, member_url in members:
        for lead_date, url in _list_lead_files(member_url):
            all_tasks.append((member_idx, lead_date, url))

    local_paths: Dict[int, Dict[str, Path]] = {idx: {} for idx, _ in members}

    def _fetch(item: Tuple[int, str, str]) -> Tuple[int, str, Optional[Path]]:
        member_idx, lead_date, url = item
        member_dir = tmpdir / f"ens{member_idx}"
        member_dir.mkdir(parents=True, exist_ok=True)
        dest = member_dir / f"{lead_date}_{Path(url).name}"
        ok = _download_with_retry(url, dest)
        return member_idx, lead_date, (dest if ok else None)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_fetch, item) for item in all_tasks]
        for fut in as_completed(futures):
            member_idx, lead_date, path = fut.result()
            if path is not None:
                local_paths[member_idx][lead_date] = path
    return local_paths


def _existing_member_indices(out_files: Dict[str, Path]) -> set:
    """Member indices already present in every existing output file for this
    date (intersection across vars -- a member only counts as 'already good'
    if it's present for both pr and tas)."""
    common: Optional[set] = None
    for path in out_files.values():
        if not path.exists():
            return set()
        try:
            ds = xr.open_dataset(path)
            idx = set(int(v) for v in ds["M"].values)
            ds.close()
        except Exception:
            return set()
        common = idx if common is None else (common & idx)
    return common or set()


def _load_existing_members(
    out_files: Dict[str, Path], indices: set
) -> Dict[int, Dict[str, xr.DataArray]]:
    """Re-load already-good members from existing output files, in the same
    per-member {var: (M=1,L,Y,X) DataArray} shape _build_member_arrays
    produces, so they can be merged with newly-fetched members without
    re-downloading data that's already correct on disk."""
    result: Dict[int, Dict[str, xr.DataArray]] = {idx: {} for idx in indices}
    for var, path in out_files.items():
        ds = xr.open_dataset(path)
        da = ds[var]
        if "S" in da.dims:
            da = da.isel(S=0, drop=True)
        for idx in indices:
            result[idx][var] = da.sel(M=float(idx)).expand_dims(M=[float(idx)]).load()
        ds.close()
    return result


def _build_member_arrays(
    member_idx: int, local_paths: Dict[str, Path], init_date: str
) -> Optional[Dict[str, xr.DataArray]]:
    """Concat one member's lead-day files along L; return {var: (L,Y,X) DataArray}."""
    per_var: Dict[str, List[xr.DataArray]] = {v: [] for v in VARS}
    for lead_date in sorted(local_paths):
        loaded = _load_lead_day(local_paths[lead_date], init_date, lead_date)
        if loaded is None:
            continue
        for var in VARS:
            per_var[var].append(loaded[var])

    if not per_var[VARS[0]]:
        return None

    result = {}
    for var in VARS:
        da = xr.concat(per_var[var], dim="L").sortby("L")
        result[var] = da.expand_dims(M=[float(member_idx)])
    return result


def _download_one_init_date(
    date_url: str,
    init_date: str,
    rt_root: str,
    overwrite: bool,
    dry_run: bool,
    workers: int,
) -> Dict[str, int]:
    """Download all members for one init date and write pr/tas hindcast files.

    If overwrite=True and output files already exist, members already present
    in them (checked via the M coordinate) are re-used as-is instead of
    re-downloaded -- only members missing from the existing file are fetched.
    This matters for retrying a partial date (e.g. 3 of 5 members downloaded
    last time): without it, "overwrite" meant re-fetching every member from
    scratch even the ones that were already fine.
    """
    counts = {"wrote": 0, "skipped": 0, "failed": 0}

    out_files = {
        var: Path(rt_root) / MODEL_ID / "hindcast" / var / f"{var}_{MODEL_ID}_{init_date}.daily.nc"
        for var in VARS
    }
    if not overwrite and all(f.exists() for f in out_files.values()):
        print(f"  [SKIP] {init_date} (outputs exist)")
        counts["skipped"] += 1
        return counts

    members = _list_members(date_url)
    if not members:
        print(f"  [WARN] No ensemble members found at {date_url}")
        counts["failed"] += 1
        return counts

    if dry_run:
        print(f"  [DRY-RUN] {init_date}  ({len(members)} members: "
              f"{', '.join(f'ens{i}' for i, _ in members)})")
        counts["wrote"] += 1
        return counts

    existing_indices: set = set()
    member_results: Dict[int, Dict[str, xr.DataArray]] = {}
    if overwrite and all(f.exists() for f in out_files.values()):
        existing_indices = _existing_member_indices(out_files)
        if existing_indices:
            member_results = _load_existing_members(out_files, existing_indices)

    members_to_fetch = [(idx, url) for idx, url in members if idx not in existing_indices]

    if not members_to_fetch:
        print(f"  [SKIP] {init_date} (all {len(existing_indices)} available members already present)")
        counts["skipped"] += 1
        return counts

    if existing_indices:
        print(f"  [{init_date}] {len(existing_indices)} members already present "
              f"(ens{','.join(str(i) for i in sorted(existing_indices))}); "
              f"fetching {len(members_to_fetch)} more")

    with tempfile.TemporaryDirectory() as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        print(f"  [{init_date}] Downloading {len(members_to_fetch)} member(s) "
              f"({', '.join(f'ens{i}' for i, _ in members_to_fetch)})")
        local_paths_by_member = _download_members_flat(members_to_fetch, tmpdir, workers)
        for member_idx, _ in members_to_fetch:
            local_paths = local_paths_by_member.get(member_idx, {})
            if not local_paths:
                print(f"    [WARN] No lead-day files downloaded for ens{member_idx}")
                continue
            built = _build_member_arrays(member_idx, local_paths, init_date)
            if built is not None:
                member_results[member_idx] = built

    if not member_results:
        print(f"  [ERROR] No usable members for {init_date}")
        counts["failed"] += 1
        return counts

    # Union of lead offsets across members; members missing a lead are NaN-padded.
    for var in VARS:
        all_leads = sorted(set(
            int(l) for res in member_results.values() for l in res[var]["L"].values
        ))
        member_das = []
        for member_idx in sorted(member_results):
            da = member_results[member_idx][var]
            missing = sorted(set(all_leads) - set(int(l) for l in da["L"].values))
            if missing:
                print(f"    [WARN] ens{member_idx} {var} missing leads {missing}; NaN-padding")
            da = da.reindex(L=all_leads)
            member_das.append(da)

        combined = xr.concat(member_das, dim="M")
        combined = combined.expand_dims(S=[np.datetime64(pd.Timestamp(init_date), "ns")])
        combined = combined.transpose("S", "M", "L", "Y", "X")
        ds_out = combined.to_dataset(name=var)

        out_file = out_files[var]
        out_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_out = out_file.with_suffix(".tmp.nc")
        ds_out.to_netcdf(tmp_out)
        tmp_out.rename(out_file)
        print(f"  [WROTE] {out_file}")

    counts["wrote"] += 1
    return counts


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Download GMAO GEOS-S2S-3 retrospective (hindcast) t2m/precip data"
    )
    ap.add_argument("--config", required=True, help="Path to config.yaml")
    ap.add_argument("--start-date", default=None, help="First init date to include (YYYYMMDD)")
    ap.add_argument("--end-date", default=None, help="Last init date to include (YYYYMMDD)")
    ap.add_argument("--overwrite", action="store_true", help="Overwrite existing files")
    ap.add_argument("--dry-run", action="store_true", help="Preview without downloading")
    ap.add_argument("--workers", type=int, default=8,
                     help="Concurrent lead-day file downloads, shared across all "
                          "members being fetched for one init date (default: 8)")
    ap.add_argument("--sleep-dates", type=float, default=3.0,
                     help="Seconds between init dates (default: 3)")
    ap.add_argument("--max-dates", type=int, default=None,
                     help="Stop after this many init dates (for testing)")
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    rt_root = (cfg.get("paths") or {}).get("rt_root", "")
    if not rt_root:
        print("[ERROR] paths.rt_root not set in config.yaml", file=sys.stderr)
        return 1

    print("Listing available init dates from GEOS-S2S-3 retro portal...")
    try:
        all_dates = _list_init_dates(BASE_URL)
    except Exception as exc:
        print(f"[ERROR] Could not reach data portal: {exc}", file=sys.stderr)
        return 1

    if not all_dates:
        print("[WARN] No init dates found")
        return 0
    print(f"Server has {len(all_dates)} init dates: {all_dates[0]} to {all_dates[-1]}")

    selected = all_dates
    if args.start_date:
        selected = [d for d in selected if d >= args.start_date]
    if args.end_date:
        selected = [d for d in selected if d <= args.end_date]
    if args.max_dates:
        selected = selected[: args.max_dates]
    if not selected:
        print("[WARN] No init dates match the requested range")
        return 0

    print(f"Processing {len(selected)} init dates\n")

    total: Dict[str, int] = {"wrote": 0, "skipped": 0, "failed": 0}
    for i, init_date in enumerate(selected, start=1):
        date_url = urljoin(BASE_URL, f"{init_date}/")
        print(f"\n=== [{i}/{len(selected)}] {init_date} ===")

        try:
            counts = _download_one_init_date(
                date_url=date_url,
                init_date=init_date,
                rt_root=rt_root,
                overwrite=args.overwrite,
                dry_run=args.dry_run,
                workers=args.workers,
            )
        except Exception as exc:
            print(f"  [ERROR] {init_date} failed: {exc}")
            counts = {"wrote": 0, "skipped": 0, "failed": 1}
        for k in total:
            total[k] += counts[k]

        if i < len(selected) and not args.dry_run:
            time.sleep(args.sleep_dates)

    print("\n=== Complete ===")
    print(f"  wrote={total['wrote']}  skipped={total['skipped']}  failed={total['failed']}")
    return 0 if total["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
