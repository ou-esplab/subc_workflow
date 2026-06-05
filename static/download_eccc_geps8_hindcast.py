#!/usr/bin/env python3
"""Download ECCC GEPS8 reforecast (hindcast) data for missing MMDD init dates.

GEPS8 hindcast data for January-June init dates is not available via IRI/SubX
but exists on the ECCC collaboration server under reforecast/subX_refcst/.
Each subdirectory is named by forecast date (YYYYMMDD); inside it holds
hindcast runs for the same MMDD across 20 years (2001-2020), 4 members each.

  reforecast/subX_refcst/
    20260101/
      subX_reforecast_ECCC_2001010100_m01.tar  (262 MB)
      subX_reforecast_ECCC_2001010100_m02.tar
      ...
      subX_reforecast_ECCC_2020010100_m04.tar

Output files match the existing hindcast convention:
  {rt_root}/ECCC-GEPS8/hindcast/{var}/{var}_ECCC-GEPS8_{YYYYMMDD}.daily.nc
  dims: (M: 4, S: 1, L: N, Y: 181, X: 360)

Usage
-----
  # Dry-run: preview what would be downloaded (fast, no data transferred)
  python static/download_eccc_geps8_hindcast.py --config config.yaml --var pr --dry-run

  # Download all Jan-Jun dates found on the server
  python static/download_eccc_geps8_hindcast.py --config config.yaml --var pr

  # Limit to specific months
  python static/download_eccc_geps8_hindcast.py --config config.yaml --var pr --months 1 2 3

  # Overwrite existing files
  python static/download_eccc_geps8_hindcast.py --config config.yaml --var pr --overwrite
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin
from urllib.request import urlopen, urlretrieve

import numpy as np
import pandas as pd
import xarray as xr
import yaml


REFCST_BASE_URL = "https://collaboration.cmc.ec.gc.ca/cmc/CMOI/GRIB/GEPS/reforecast/subX_refcst/"
MODEL_ID = "ECCC-GEPS8"
# Reforecast members are m01-m04 (4 members, unlike realtime which is m00-m20)
MEMBERS = ["m01", "m02", "m03", "m04"]


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _http_get(url: str, timeout: int = 30) -> str:
    with urlopen(url, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def _list_refcst_dates(base_url: str) -> List[str]:
    """Return sorted list of YYYYMMDD forecast dates on the server."""
    text = _http_get(base_url)
    return sorted(set(re.findall(r'href=["\'](\d{8})/', text)))


def _list_tar_files(date_url: str) -> List[Tuple[str, str]]:
    """Return deduplicated list of (filename, full_url) for tar files in a date dir."""
    text = _http_get(date_url)
    names = re.findall(
        r'href=["\']([^"\']*subX_reforecast_ECCC_\d+_m\d+\.tar)["\']', text
    )
    seen: set = set()
    result = []
    for name in names:
        bare = name.split("/")[-1]
        if bare not in seen:
            seen.add(bare)
            result.append((bare, urljoin(date_url, name)))
    return sorted(result)


def _parse_tar_name(name: str) -> Optional[Tuple[str, str]]:
    """Extract (YYYYMMDD, member) from 'subX_reforecast_ECCC_YYYYMMDDH_mNN.tar'."""
    m = re.match(r"subX_reforecast_ECCC_(\d{8})\d{2}_(m\d{2})\.tar$", name)
    if not m:
        return None
    return m.group(1), m.group(2)


# ── Download helpers ──────────────────────────────────────────────────────────

def _download_with_retry(
    url: str, dest: Path, retries: int = 3, sleep_secs: float = 5.0
) -> bool:
    """Download url to dest atomically, with exponential-backoff retries."""
    tmp = dest.with_suffix(".download.tmp")
    for attempt in range(retries):
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            urlretrieve(url, str(tmp))
            tmp.rename(dest)
            return True
        except Exception as exc:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            wait = sleep_secs * (2 ** attempt)
            print(f"    [WARN] Attempt {attempt + 1}/{retries} failed: {exc}")
            if attempt < retries - 1:
                print(f"    Retrying in {wait:.0f}s...")
                time.sleep(wait)
    return False


def _extract_var_from_tar(tar_path: Path, var: str, dest: Path) -> bool:
    """Extract the first NetCDF file containing var from a tar archive."""
    var_re = re.compile(rf"(^|[^a-z0-9]){re.escape(var.lower())}([^a-z0-9]|$)")
    with tarfile.open(str(tar_path), mode="r:*") as tf:
        for ti in tf.getmembers():
            if not ti.isfile():
                continue
            base = os.path.basename(ti.name).lower()
            if not (base.endswith(".nc") or base.endswith(".nc4")):
                continue
            if not var_re.search(base):
                continue
            extracted = tf.extractfile(ti)
            if extracted is None:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as f:
                f.write(extracted.read())
            return True
    return False


# ── NetCDF conversion ─────────────────────────────────────────────────────────

_DIM_ALIASES = {
    "latitude": "Y", "lat": "Y",
    "longitude": "X", "lon": "X",
    "time": "L",
}


def _nc_to_member_array(
    nc_path: Path, var: str, hc_date: str, m_idx: int
) -> Optional[xr.DataArray]:
    """Read extracted NC, normalise dims, compute lead days, return M-expanded DataArray."""
    init_ts = pd.Timestamp(hc_date)
    ds = xr.open_dataset(nc_path)

    if var not in ds:
        print(f"    [WARN] '{var}' not found in {nc_path.name}; data_vars={list(ds.data_vars)}")
        ds.close()
        return None

    # Rename dims to canonical Y/X/L, handling lat/latitude etc.
    rename = {k: v for k, v in _DIM_ALIASES.items() if k in ds.dims or k in ds.coords}
    if rename:
        ds = ds.rename(rename)

    # Compute lead days relative to init_date.
    # Prefer reference_date attr if present; fall back to hc_date.
    ref_str = ds["L"].attrs.get("reference_date", "") if "L" in ds else ""
    if not ref_str:
        ref_str = ds[list(ds.dims)[0]].attrs.get("reference_date", "") if ds.dims else ""
    ref_ts = pd.Timestamp(ref_str.replace(" UTC", "")) if ref_str else init_ts

    time_vals = pd.DatetimeIndex(ds["L"].values)
    lead_days = np.array(
        [(t - ref_ts).total_seconds() / 86400.0 for t in time_vals],
        dtype=np.float32,
    )

    da = ds[var].assign_coords(L=lead_days)
    da["L"].attrs = {"units": "days"}
    da = da.expand_dims(M=[float(m_idx)])
    ds.close()
    return da


# ── Per-date download logic ───────────────────────────────────────────────────

def _download_one_fcst_date(
    date_url: str,
    var: str,
    rt_root: str,
    overwrite: bool,
    dry_run: bool,
    sleep_between_members: float,
    sleep_between_years: float,
) -> Dict[str, int]:
    """Download all hindcast-year × member tars for one MMDD forecast date.

    Returns count dict: {wrote, skipped, failed}.
    """
    counts = {"wrote": 0, "skipped": 0, "failed": 0}

    tars = _list_tar_files(date_url)
    if not tars:
        print(f"  [WARN] No tar files found at {date_url}")
        return counts

    # Group tars by hindcast YYYYMMDD: {hc_date: [(member, url, name), ...]}
    by_year: Dict[str, List[Tuple[str, str, str]]] = {}
    for name, url in tars:
        parsed = _parse_tar_name(name)
        if not parsed:
            print(f"  [WARN] Cannot parse tar name: {name}")
            continue
        hc_date, member = parsed
        by_year.setdefault(hc_date, []).append((member, url, name))

    for year_idx, hc_date in enumerate(sorted(by_year.keys())):
        out_file = (
            Path(rt_root)
            / MODEL_ID / "hindcast" / var
            / f"{var}_{MODEL_ID}_{hc_date}.daily.nc"
        )

        if out_file.exists() and not overwrite:
            print(f"  [SKIP] {out_file.name}")
            counts["skipped"] += 1
            continue

        if dry_run:
            member_list = [m for m, _, _ in by_year[hc_date]]
            print(f"  [DRY-RUN] {out_file.name}  ({len(member_list)} members: {', '.join(member_list)})")
            counts["wrote"] += 1
            continue

        member_entries = sorted(by_year[hc_date], key=lambda x: x[0])
        member_arrays: List[xr.DataArray] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            for m_idx, (member, url, tar_name) in enumerate(member_entries, start=1):
                tar_path = Path(tmpdir) / tar_name
                nc_path = Path(tmpdir) / f"{var}_{member}.nc"

                print(f"  [{hc_date}] Downloading {tar_name} ({m_idx}/{len(member_entries)})")
                ok = _download_with_retry(url, tar_path, sleep_secs=sleep_between_members)
                if not ok:
                    print(f"  [ERROR] Failed to download {tar_name}")
                    counts["failed"] += 1
                    continue

                ok = _extract_var_from_tar(tar_path, var, nc_path)
                if not ok:
                    print(f"  [WARN] '{var}' not extractable from {tar_name}")
                    counts["failed"] += 1
                    continue

                da = _nc_to_member_array(nc_path, var, hc_date, m_idx)
                if da is None:
                    counts["failed"] += 1
                    continue

                member_arrays.append(da)
                tar_path.unlink(missing_ok=True)  # free disk space before next member

                # Be nice: pause between members of the same year
                if m_idx < len(member_entries) and not dry_run:
                    time.sleep(sleep_between_members)

        if not member_arrays:
            print(f"  [ERROR] No usable members for {hc_date}")
            counts["failed"] += 1
            continue

        # Merge members and write atomically
        combined = xr.concat(member_arrays, dim="M")
        combined = combined.expand_dims(
            S=[np.datetime64(pd.Timestamp(hc_date), "ns")]
        )
        ds_out = combined.to_dataset(name=var)

        tmp_out = out_file.with_suffix(".tmp.nc")
        ds_out.to_netcdf(tmp_out)
        tmp_out.rename(out_file)
        print(f"  [WROTE] {out_file.name}")
        counts["wrote"] += 1

        # Pause between hindcast years within the same forecast date
        if year_idx < len(by_year) - 1 and not dry_run:
            time.sleep(sleep_between_years)

    return counts


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Download ECCC GEPS8 reforecast hindcast data for Jan-Jun init dates"
    )
    ap.add_argument("--config",   required=True,  help="Path to config.yaml")
    ap.add_argument("--var",      required=True,  help="Variable (e.g. pr, tas)")
    ap.add_argument("--months",   type=int, nargs="+", default=None,
                    help="Months to download 1-12 (default: 1-6 i.e. Jan-Jun)")
    ap.add_argument("--overwrite", action="store_true", help="Overwrite existing files")
    ap.add_argument("--dry-run",   action="store_true", help="Preview without downloading")
    ap.add_argument("--sleep-members", type=float, default=3.0,
                    help="Seconds between member downloads within one year (default: 3)")
    ap.add_argument("--sleep-years", type=float, default=5.0,
                    help="Seconds between hindcast years within one forecast date (default: 5)")
    ap.add_argument("--sleep-dates", type=float, default=15.0,
                    help="Seconds between forecast dates (default: 15)")
    ap.add_argument("--max-dates", type=int, default=None,
                    help="Stop after this many forecast dates (for testing)")
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    rt_root = (cfg.get("paths") or {}).get("rt_root", "")
    if not rt_root:
        print("[ERROR] paths.rt_root not set in config.yaml", file=sys.stderr)
        return 1

    target_months = set(args.months) if args.months else set(range(1, 7))

    print(f"Listing available reforecast dates from ECCC server...")
    try:
        all_dates = _list_refcst_dates(REFCST_BASE_URL)
    except Exception as exc:
        print(f"[ERROR] Could not reach ECCC server: {exc}", file=sys.stderr)
        return 1

    print(f"Server has {len(all_dates)} forecast dates: {all_dates[0]} to {all_dates[-1]}")

    selected = [d for d in all_dates if int(d[4:6]) in target_months]
    if args.max_dates:
        selected = selected[: args.max_dates]
    if not selected:
        print(f"[WARN] No dates found for months {sorted(target_months)}")
        return 0

    print(f"Downloading {len(selected)} forecast dates for months {sorted(target_months)}")
    print(f"Sleeps: {args.sleep_members}s between members, "
          f"{args.sleep_years}s between years, "
          f"{args.sleep_dates}s between forecast dates\n")

    total: Dict[str, int] = {"wrote": 0, "skipped": 0, "failed": 0}

    for i, fcst_date in enumerate(selected, start=1):
        date_url = urljoin(REFCST_BASE_URL, f"{fcst_date}/")
        print(f"\n=== [{i}/{len(selected)}] {fcst_date} ===")

        counts = _download_one_fcst_date(
            date_url=date_url,
            var=args.var,
            rt_root=rt_root,
            overwrite=args.overwrite,
            dry_run=args.dry_run,
            sleep_between_members=args.sleep_members,
            sleep_between_years=args.sleep_years,
        )
        for k in total:
            total[k] += counts[k]

        if i < len(selected):
            print(f"\n  Pausing {args.sleep_dates:.0f}s before next forecast date...")
            time.sleep(args.sleep_dates)

    print(f"\n=== Complete ===")
    print(f"  wrote={total['wrote']}  skipped={total['skipped']}  failed={total['failed']}")
    return 0 if total["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
