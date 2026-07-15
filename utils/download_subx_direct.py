#!/usr/bin/env python3
"""Download SubX files directly from provider endpoints.

This utility discovers candidate files from a provider endpoint, filters by
variable and init-date lookback window, and writes normalized local output
filenames that match the existing workflow contract.
"""

import argparse
import os
import re
import sys
import tarfile
import tempfile
from datetime import datetime, timedelta
from ftplib import FTP
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlparse
from urllib.request import urlopen, urlretrieve

import yaml


DEFAULT_PROVIDER_URLS = {
    "esrl": "ftp://gsdftp.fsl.noaa.gov/SubX-ESRL-FIMr1.1/",
    "gmao": "https://gmao.gsfc.nasa.gov/gmaoftp/gmaofcst/subx/GEOS_S2S_V2.1_fcst/",
    "gmao_v3": "https://portal.nccs.nasa.gov/datashare/gmao/geos-s2s-3/NRT/SubC/",
    "eccc": "https://collaboration.cmc.ec.gc.ca/cmc/CMOI/GRIB/GEPS/forecast/subX_fcst/",
    "rsmas": "ftp://decadal.rsmas.miami.edu/pub/CPC_DATA/CCSM4/forecast/priority1/",
    "cfs": "https://ftp.cpc.ncep.noaa.gov/dcollins/SubX/CFS/",
    "gefs": "https://ftp.cpc.ncep.noaa.gov/dcollins/SubX/GEFS/",
}


class RemoteEntry:
    def __init__(self, name, url):
        self.name = name
        self.url = url


class CandidateEntry:
    def __init__(self, init_date, entry, from_tar=False):
        self.init_date = init_date
        self.entry = entry
        self.from_tar = from_tar


def _load_cfg(path: Optional[str]) -> Dict:
    if not path or not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _cfg_provider_url(cfg: Dict, provider: str) -> str:
    direct_cfg = ((cfg.get("ingest") or {}).get("direct") or {})
    providers = direct_cfg.get("providers") or {}
    provider_cfg = providers.get(provider) or {}
    return provider_cfg.get("url") or DEFAULT_PROVIDER_URLS[provider]


def _cfg_lookback_days(cfg: Dict) -> int:
    ingest_cfg = (cfg.get("ingest") or {})
    if isinstance(ingest_cfg.get("lookback_days"), int):
        return max(1, int(ingest_cfg["lookback_days"]))
    val_cfg = (cfg.get("validation") or {})
    if isinstance(val_cfg.get("lookback_days"), int):
        return max(1, int(val_cfg["lookback_days"]))
    return 7


def _cfg_eccc_members(cfg: Dict) -> List[str]:
    direct_cfg = ((cfg.get("ingest") or {}).get("direct") or {})
    eccc_cfg = direct_cfg.get("eccc") or {}
    raw = eccc_cfg.get("member")

    def default_members() -> List[str]:
        return ["m%02d" % i for i in range(21)]

    if raw is None:
        return default_members()

    if isinstance(raw, str):
        token = raw.strip().lower()
        range_match = re.fullmatch(r"m(\d{2})-m(\d{2})", token)
        if range_match:
            start = int(range_match.group(1))
            end = int(range_match.group(2))
            if start <= end:
                return ["m%02d" % i for i in range(start, end + 1)]
        if re.fullmatch(r"m\d{2}", token):
            return [token]
        return default_members()

    if isinstance(raw, list):
        members = []
        for item in raw:
            token = str(item).strip().lower()
            if re.fullmatch(r"m\d{2}", token):
                members.append(token)
        return members or default_members()

    return default_members()


def _cfg_eccc_var_levels(cfg: Dict) -> Dict[str, int]:
    """Return per-variable pressure level overrides for ECCC tar extraction.

    Maps CF var name (e.g. 'ua') to the integer hPa level to select from the
    per-level files inside the ECCC tar (e.g. ua_200_ECCC_...).  Configured
    under ingest.direct.eccc.var_levels in config.yaml.
    """
    direct_cfg = ((cfg.get("ingest") or {}).get("direct") or {})
    eccc_cfg = direct_cfg.get("eccc") or {}
    raw = eccc_cfg.get("var_levels") or {}
    return {str(k): int(v) for k, v in raw.items()}


def _cfg_provider_var_levels(cfg: Dict, provider: str) -> Dict[str, int]:
    """Return per-variable pressure level overrides for a provider's file listing.

    Some providers (e.g. RSMAS) publish separate single-level files per
    variable (ua_200_*, ua_850_*, ...) without a dedicated per-level fetcher;
    this tells the generic candidate filter which level's file to pick.
    Configured under ingest.direct.<provider>.var_levels in config.yaml.
    """
    direct_cfg = ((cfg.get("ingest") or {}).get("direct") or {})
    provider_cfg = direct_cfg.get(provider) or {}
    raw = provider_cfg.get("var_levels") or {}
    return {str(k): int(v) for k, v in raw.items()}


def _ftp_email(cfg: Dict) -> str:
    env_email = os.environ.get("SUBX_DIRECT_FTP_EMAIL", "").strip()
    if env_email:
        return env_email
    direct_cfg = ((cfg.get("ingest") or {}).get("direct") or {})
    cfg_email = str(direct_cfg.get("ftp_email") or "").strip()
    if cfg_email:
        return cfg_email
    return "anonymous@example.com"


def _lookback_dates(fcst: str, lookback_days: int) -> List[str]:
    end = datetime.strptime(fcst, "%Y%m%d")
    return [(end - timedelta(days=delta)).strftime("%Y%m%d") for delta in range(max(1, lookback_days))]


def _extract_dates(text: str) -> List[str]:
    dates = []

    # Generic YYYYMMDD tokens.
    dates.extend(re.findall(r"(20\d{6})", text))

    # GMAO tokens like 31may2026 -> 20260531.
    for token in re.findall(r"(\d{2}[a-z]{3}\d{4})", text.lower()):
        try:
            dt = datetime.strptime(token, "%d%b%Y")
            dates.append(dt.strftime("%Y%m%d"))
        except Exception:
            continue

    return dates


def _var_regex(var):
    return re.compile(rf"(^|[^a-z0-9]){re.escape(var.lower())}([^a-z0-9]|$)")


def _looks_like_data_file(name: str) -> bool:
    lower = name.lower()
    return lower.endswith(".nc") or lower.endswith(".nc4") or lower.endswith(".grb") or lower.endswith(".grib") or lower.endswith(".grib2")


def _looks_like_tar_file(name: str) -> bool:
    return name.lower().endswith(".tar")


def _is_date_dir_name(name: str) -> bool:
    return bool(re.fullmatch(r"20\d{6}", name))


def _http_list(url: str, depth: int = 1) -> List[RemoteEntry]:
    visited = set()
    entries: List[RemoteEntry] = []

    def walk(base_url: str, remaining: int) -> None:
        if base_url in visited:
            return
        visited.add(base_url)
        try:
            with urlopen(base_url, timeout=30) as resp:
                text = resp.read().decode("utf-8", errors="ignore")
        except Exception:
            return

        hrefs = re.findall(r'href=["\']([^"\']+)["\']', text, flags=re.IGNORECASE)
        for href in hrefs:
            if href.startswith("?") or href.startswith("#"):
                continue
            child = urljoin(base_url, href)
            child_name = href.split("/")[-1] if not href.endswith("/") else href.rstrip("/").split("/")[-1]
            if not child_name:
                continue
            if href.endswith("/") and remaining > 0:
                walk(child, remaining - 1)
            else:
                entries.append(RemoteEntry(name=child_name, url=child))

    walk(url, depth)
    return entries


def _http_list_eccc(url: str, valid_dates: Sequence[str]) -> List[RemoteEntry]:
    entries: List[RemoteEntry] = []
    valid_date_set = set(valid_dates)

    try:
        with urlopen(url, timeout=30) as resp:
            root_text = resp.read().decode("utf-8", errors="ignore")
    except Exception as exc:
        print(f"[DIRECT][WARN] Failed listing ECCC root {url}: {exc}", file=sys.stderr)
        return entries

    hrefs = re.findall(r'href=["\']([^"\']+)["\']', root_text, flags=re.IGNORECASE)
    subdirs: List[Tuple[str, str]] = []
    for href in hrefs:
        if href.startswith("?") or href.startswith("#"):
            continue
        if not href.endswith("/"):
            continue
        dir_name = href.rstrip("/").split("/")[-1]
        if not _is_date_dir_name(dir_name):
            continue
        if dir_name not in valid_date_set:
            continue
        subdirs.append((dir_name, urljoin(url, href)))

    for _, subdir_url in sorted(subdirs, reverse=True):
        try:
            with urlopen(subdir_url, timeout=30) as resp:
                text = resp.read().decode("utf-8", errors="ignore")
        except Exception as exc:
            print(f"[DIRECT][WARN] Failed listing ECCC date dir {subdir_url}: {exc}", file=sys.stderr)
            continue

        child_hrefs = re.findall(r'href=["\']([^"\']+)["\']', text, flags=re.IGNORECASE)
        for href in child_hrefs:
            if href.startswith("?") or href.startswith("#"):
                continue
            if href.endswith("/"):
                continue
            child = urljoin(subdir_url, href)
            child_name = href.split("/")[-1]
            if not child_name:
                continue
            entries.append(RemoteEntry(name=child_name, url=child))

    return entries


def _http_list_gmao_v3(url: str, valid_dates: Sequence[str]) -> List[RemoteEntry]:
    entries: List[RemoteEntry] = []
    valid_date_set = set(valid_dates)

    try:
        with urlopen(url, timeout=30) as resp:
            root_text = resp.read().decode("utf-8", errors="ignore")
    except Exception as exc:
        print(f"[DIRECT][WARN] Failed listing GMAO V3 root {url}: {exc}", file=sys.stderr)
        return entries

    hrefs = re.findall(r'href=["\']([^"\']+)["\']', root_text, flags=re.IGNORECASE)
    subdirs: List[Tuple[str, str]] = []
    for href in hrefs:
        if href.startswith("?") or href.startswith("#"):
            continue
        if not href.endswith("/"):
            continue
        dir_name = href.rstrip("/").split("/")[-1]
        if not _is_date_dir_name(dir_name):
            continue
        if dir_name not in valid_date_set:
            continue
        subdirs.append((dir_name, urljoin(url, href)))

    for _, subdir_url in sorted(subdirs, reverse=True):
        try:
            with urlopen(subdir_url, timeout=30) as resp:
                text = resp.read().decode("utf-8", errors="ignore")
        except Exception as exc:
            print(f"[DIRECT][WARN] Failed listing GMAO V3 date dir {subdir_url}: {exc}", file=sys.stderr)
            continue

        child_hrefs = re.findall(r'href=["\']([^"\']+)["\']', text, flags=re.IGNORECASE)
        for href in child_hrefs:
            if href.startswith("?") or href.startswith("#"):
                continue
            if href.endswith("/"):
                continue
            child = urljoin(subdir_url, href)
            child_name = href.split("/")[-1]
            if not child_name:
                continue
            entries.append(RemoteEntry(name=child_name, url=child))

    return entries


def _ftp_list(url: str, email: str) -> List[RemoteEntry]:
    parsed = urlparse(url)
    if parsed.scheme != "ftp" or not parsed.hostname:
        return []

    base_path = parsed.path or "/"
    if not base_path.endswith("/"):
        base_path += "/"

    entries: List[RemoteEntry] = []
    ftp = FTP(parsed.hostname, timeout=30)
    try:
        _ftp_login_with_fallback(ftp, email)
        ftp.cwd(base_path)
        names = ftp.nlst()
    except Exception as exc:
        print(f"[DIRECT][WARN] FTP listing failed for {url}: {exc}", file=sys.stderr)
        names = []
    finally:
        try:
            ftp.quit()
        except Exception:
            pass

    for name in names:
        clean = name.split("/")[-1]
        if not clean:
            continue
        full_path = f"{base_path.rstrip('/')}/{clean}"
        entries.append(RemoteEntry(name=clean, url=f"ftp://{parsed.hostname}{full_path}"))
    return entries


def _ftp_login_with_fallback(ftp: FTP, email: str) -> None:
    attempts = [
        ("anonymous", email),
        ("anonymous", "anonymous@example.com"),
        ("anonymous", "anonymous"),
        ("ftp", "ftp"),
    ]
    seen = set()
    last_exc = None

    for user, passwd in attempts:
        key = (user, passwd)
        if key in seen:
            continue
        seen.add(key)
        try:
            ftp.login(user, passwd)
            return
        except Exception as exc:  # pragma: no cover - exercised via unit tests
            last_exc = exc

    if last_exc:
        raise last_exc
    raise RuntimeError("FTP login failed with no exception details")


def _list_entries(provider_url: str, ftp_email: str, provider: str, valid_dates: Sequence[str]) -> List[RemoteEntry]:
    scheme = urlparse(provider_url).scheme.lower()
    if scheme in {"http", "https"}:
        if provider == "eccc":
            return _http_list_eccc(provider_url, valid_dates)
        if provider == "gmao_v3":
            return _http_list_gmao_v3(provider_url, valid_dates)
        return _http_list(provider_url, depth=1)
    if scheme == "ftp":
        return _ftp_list(provider_url, ftp_email)
    return []


def _filter_candidates(entries: Sequence[RemoteEntry], var: str, valid_dates: Sequence[str], provider: str) -> List[CandidateEntry]:
    var_re = _var_regex(var)
    valid_date_set = set(valid_dates)
    candidates: List[CandidateEntry] = []
    for entry in entries:
        name = entry.name
        lower = name.lower()
        is_tar = _looks_like_tar_file(name)
        if not _looks_like_data_file(name) and not is_tar:
            continue

        if not is_tar and not var_re.search(lower):
            continue
        if is_tar and provider != "eccc":
            continue

        # ECCC tar archives may not include variable token in tar filename.
        if is_tar and provider == "eccc" and var_re.search(lower):
            pass

        date_tokens = _extract_dates(name)
        if not date_tokens:
            date_tokens = _extract_dates(entry.url)
        match_date = next((d for d in date_tokens if d in valid_date_set), None)
        if not match_date:
            continue
        candidates.append(CandidateEntry(init_date=match_date, entry=entry, from_tar=is_tar))

    # Newest init first; stable secondary sort by name.
    candidates.sort(key=lambda item: (item.init_date, item.entry.name), reverse=True)
    return candidates


def _extract_member_token(text: str) -> str:
    match = re.search(r"\bm\d{2}\b", text.lower())
    return match.group(0) if match else ""


def _extract_from_tar(
    tar_path: Path,
    destination: Path,
    var: str,
    preferred_members: Sequence[str],
    preferred_level: Optional[int] = None,
) -> bool:
    var_re = _var_regex(var)
    member_rank = {m.lower(): i for i, m in enumerate(preferred_members)}
    fallback_rank = len(member_rank) + 100
    # Level token to prefer when tar contains per-level files (e.g. ua_200_ECCC_...).
    level_token = f"{var}_{preferred_level}_" if preferred_level is not None else None

    with tarfile.open(str(tar_path), mode="r:*") as tar:
        members = [m for m in tar.getmembers() if m.isfile()]
        candidates = []
        for member in members:
            base = os.path.basename(member.name).lower()
            if not var_re.search(base):
                continue
            ext_ok = _looks_like_data_file(base)
            if not ext_ok:
                continue
            member_token = _extract_member_token(base)
            rank = member_rank.get(member_token, fallback_rank)
            # Give the preferred-level file the highest priority (rank -1).
            if level_token is not None and base.startswith(level_token):
                rank = -1
            candidates.append((rank, member.name, member))

        if not candidates:
            return False

        # Preferred level first, then preferred members in order, then name.
        candidates.sort(key=lambda item: (item[0], item[1]))
        selected = candidates[0][2]
        extracted = tar.extractfile(selected)
        if extracted is None:
            return False

        destination.parent.mkdir(parents=True, exist_ok=True)
        with open(destination, "wb") as out_f:
            out_f.write(extracted.read())

    return True


def _materialize_candidate(
    candidate: CandidateEntry,
    out_file: Path,
    ftp_email: str,
    var: str,
    preferred_members: Sequence[str],
) -> bool:
    if not candidate.from_tar:
        _download_file(candidate.entry.url, out_file, ftp_email)
        return True

    with tempfile.TemporaryDirectory() as tmpdir:
        tar_path = Path(tmpdir) / candidate.entry.name
        _download_file(candidate.entry.url, tar_path, ftp_email)
        return _extract_from_tar(tar_path, out_file, var, preferred_members)


def _download_file(source_url: str, destination: Path, ftp_email: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    parsed = urlparse(source_url)
    scheme = parsed.scheme.lower()

    if scheme in {"http", "https"}:
        urlretrieve(source_url, str(destination))
        return

    if scheme == "ftp" and parsed.hostname:
        ftp = FTP(parsed.hostname, timeout=60)
        try:
            _ftp_login_with_fallback(ftp, ftp_email)
            remote_path = parsed.path
            with open(destination, "wb") as f:
                ftp.retrbinary(f"RETR {remote_path}", f.write)
        finally:
            try:
                ftp.quit()
            except Exception:
                pass
        return

    raise RuntimeError(f"Unsupported source URL: {source_url}")


def _snap_grid_coords(ds: "xr.Dataset") -> "xr.Dataset":
    """Round lat/lon coordinates to the nearest integer degree.

    Some provider sources encode grid coordinates with tiny floating-point
    error baked into the file itself (e.g. ESRL's rlut/tas/ts/ua/va files
    carry -89.99999237 instead of -90.0, while its own pr/zg files are
    exact). SubX's grid is always exact 1-degree spacing, so snapping at
    download time prevents spurious near-duplicate points once datasets are
    later merged across variables/models.
    """
    import numpy as np
    names = [n for n in ("lat", "lon", "LAT", "LON") if n in ds.coords]
    coords = {name: np.round(ds[name].values) for name in names}
    return ds.assign_coords(**coords) if coords else ds


# ── ESRL-specific download helpers ────────────────────────────────────────────

# Single-level variables: SubX var name → ESRL FTP filename prefix.
_ESRL_VAR_PREFIX: Dict[str, str] = {
    "pr":   "pr_sfc",
    "rlut": "rlut_toa",
    "tas":  "tas_2m",
    "ts":   "ts_sfc",
    "psl":  "psl_sfc",
}

# Multi-level variables: SubX var → ordered list of (pressure_hPa, filename_prefix).
# Order matches the P coordinate in the primary iridl files.
_ESRL_VAR_LEVELS: Dict[str, List[Tuple[int, str]]] = {
    "zg": [(850, "zg_850"), (500, "zg_500"), (200, "zg_200"), (50, "zg_50"), (30, "zg_30"), (10, "zg_10")],
    "ua": [(850, "ua_850"), (200, "ua_200"), (100, "ua_100"), (50, "ua_50"), (30, "ua_30"), (10, "ua_10")],
    "va": [(850, "va_850"), (200, "va_200"), (100, "va_100"), (50, "va_50"), (30, "va_30"), (10, "va_10")],
}

_ESRL_MEMBERS = ["m01", "m02", "m03", "m04"]


def _esrl_date_str(init_date: str) -> str:
    """Convert YYYYMMDD to ESRL FTP date token (e.g. '20260603' → '03jun2026')."""
    return datetime.strptime(init_date, "%Y%m%d").strftime("%d%b%Y").lower()


def _list_esrl_dates(ftp_url: str, ftp_email: str, var: str) -> List[str]:
    """Return available init dates for a variable on the ESRL FTP, newest first."""
    # Use the first level prefix as the sentinel for multi-level vars
    if var in _ESRL_VAR_LEVELS:
        sentinel_prefix = _ESRL_VAR_LEVELS[var][0][1].lower()
    else:
        sentinel_prefix = _ESRL_VAR_PREFIX.get(var, "").lower()
    if not sentinel_prefix:
        return []
    entries = _ftp_list(ftp_url, ftp_email)
    dates: set = set()
    for entry in entries:
        name = entry.name.lower()
        # Match only member-01 sentinel to avoid duplicates: {prefix}_FIM_*_m01.nc
        if not (name.startswith(sentinel_prefix + "_fim_") and name.endswith("_m01.nc")):
            continue
        for d in _extract_dates(entry.name):
            dates.add(d)
    return sorted(dates, reverse=True)


def _download_esrl_to_subx(
    ftp_url: str,
    ftp_email: str,
    var: str,
    init_date: str,
    out_file: Path,
) -> bool:
    """Download all 4 ESRL ensemble member files, merge into SubX format, save.

    Output dimensions: (S: 1, M: 4, L: 32, Y: 181, X: 360)
    Lead coordinate L: float days offset from init date (e.g. -0.5, 0.5, … 30.5)
    """
    import numpy as np
    import pandas as pd
    import xarray as xr

    is_multilevel = var in _ESRL_VAR_LEVELS
    var_prefix = _ESRL_VAR_PREFIX.get(var)
    if not is_multilevel and var_prefix is None:
        print(f"[DIRECT][ESRL] Variable '{var}' not available on ESRL FTP; skipping.")
        return False

    esrl_date = _esrl_date_str(init_date)
    init_ts = pd.Timestamp(init_date)
    parsed = urlparse(ftp_url)
    base_path = parsed.path.rstrip("/")

    member_arrays: List = []
    lead_days_ref: Optional[np.ndarray] = None  # pin L to member-1 values

    def _fetch_one(prefix: str, member: str, tmpdir: str) -> "xr.Dataset":
        """Download one per-member file and return as an open dataset."""
        fname = f"{prefix}_FIM_{esrl_date}_00z_d01_d32_{member}.nc"
        remote_path = f"{base_path}/{fname}"
        tmp_file = Path(tmpdir) / fname
        print(f"[DIRECT][ESRL] Downloading ftp://{parsed.hostname}{remote_path}")
        ftp = FTP(parsed.hostname, timeout=60)
        try:
            _ftp_login_with_fallback(ftp, ftp_email)
            with open(tmp_file, "wb") as f:
                ftp.retrbinary(f"RETR {remote_path}", f.write)
        except Exception as exc:
            raise RuntimeError(f"FTP download failed for {fname}: {exc}") from exc
        finally:
            try:
                ftp.quit()
            except Exception:
                pass
        # ESRL uses Julian calendar; decode manually to avoid cftime issues
        return _snap_grid_coords(xr.open_dataset(tmp_file, decode_times=False))

    def _lead_days_from_ds(ds: "xr.Dataset") -> "np.ndarray":
        time_units = ds["time"].attrs.get("units", "")
        m = re.match(r"days since (.+)", time_units.strip())
        if not m:
            raise ValueError(f"Unrecognised ESRL time units: {time_units!r}")
        base_time = pd.Timestamp(m.group(1))
        raw_days = ds["time"].values.astype(float)
        return np.array(
            [(base_time + pd.Timedelta(days=float(d)) - init_ts).total_seconds() / 86400.0
             for d in raw_days],
            dtype=np.float32,
        )

    def _extract_var(ds: "xr.Dataset", fname: str) -> "xr.DataArray":
        if var in ds:
            return ds[var]
        if "air_temperature" in ds:
            print(f"[DIRECT][ESRL] Renaming 'air_temperature' -> '{var}' in {fname}")
            return ds["air_temperature"]
        raise KeyError(
            f"Neither '{var}' nor 'air_temperature' found in {fname}. "
            f"Available: {list(ds.data_vars)}"
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        for m_idx, member in enumerate(_ESRL_MEMBERS, start=1):
            try:
                if is_multilevel:
                    # Download one file per pressure level, stack into P dim
                    level_arrays: List = []
                    for p_val, prefix in _ESRL_VAR_LEVELS[var]:
                        ds = _fetch_one(prefix, member, tmpdir)
                        if lead_days_ref is None:
                            lead_days_ref = _lead_days_from_ds(ds)
                        da_lev = (
                            _extract_var(ds, f"{prefix}_FIM_{esrl_date}_00z_d01_d32_{member}.nc")
                            .rename({"time": "L", "lat": "Y", "lon": "X"})
                            .assign_coords(L=lead_days_ref)
                        )
                        da_lev["L"].attrs["units"] = "days"
                        da_lev = da_lev.expand_dims(P=[p_val])
                        level_arrays.append(da_lev)
                        ds.close()
                    da_member = xr.concat(level_arrays, dim="P").expand_dims(M=[m_idx])
                else:
                    # Single-level variable
                    ds = _fetch_one(var_prefix, member, tmpdir)
                    if lead_days_ref is None:
                        lead_days_ref = _lead_days_from_ds(ds)
                    fname = f"{var_prefix}_FIM_{esrl_date}_00z_d01_d32_{member}.nc"
                    da_member = (
                        _extract_var(ds, fname)
                        .rename({"time": "L", "lat": "Y", "lon": "X"})
                        .assign_coords(L=lead_days_ref)
                    )
                    da_member["L"].attrs["units"] = "days"
                    da_member = da_member.expand_dims(M=[m_idx])
                    ds.close()
            except Exception as exc:
                print(f"[DIRECT][ESRL][WARN] Failed for member {member}: {exc}", file=sys.stderr)
                return False
            member_arrays.append(da_member)

        combined = xr.concat(member_arrays, dim="M")
        combined = combined.expand_dims(S=[np.datetime64(init_ts, "ns")])
        ds_out = combined.to_dataset(name=var)

        out_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_file.with_suffix(".tmp.nc")
        ds_out.to_netcdf(tmp)
        tmp.rename(out_file)

    return True


# ── end ESRL helpers ───────────────────────────────────────────────────────────


# ── GMAO-specific download helpers ────────────────────────────────────────────
# GMAO file pattern: {prefix}_GMAOGEOS_{ddmonyyyy}_00z_d01_d45_{member}.nc
# Same per-member structure as ESRL; 4 members (m01-m04).

_GMAO_VAR_PREFIX: Dict[str, str] = {
    "pr":   "pr_sfc",
    "rlut": "rlut_toa",
    "tas":  "tas_2m",
    "ts":   "ts_sfc",
}

_GMAO_VAR_LEVELS: Dict[str, List[Tuple[int, str]]] = {
    "ua": [(200, "ua_200"), (850, "ua_850")],
    "va": [(200, "va_200"), (850, "va_850")],
    "zg": [(200, "zg_200"), (500, "zg_500")],
}

_GMAO_MEMBERS = ["m01", "m02", "m03", "m04"]


def _list_gmao_dates(http_url: str, var: str) -> List[str]:
    """Return available init dates for a variable on the GMAO HTTP server, newest first."""
    if var in _GMAO_VAR_LEVELS:
        sentinel_prefix = _GMAO_VAR_LEVELS[var][0][1].lower()
    else:
        sentinel_prefix = _GMAO_VAR_PREFIX.get(var, "").lower()
    if not sentinel_prefix:
        return []
    entries = _http_list(http_url, depth=1)
    dates: set = set()
    for entry in entries:
        name = entry.name.lower()
        if not (name.startswith(sentinel_prefix + "_gmaogeos_") and name.endswith("_m01.nc")):
            continue
        for d in _extract_dates(entry.name):
            dates.add(d)
    return sorted(dates, reverse=True)


def _download_gmao_to_subx(
    http_url: str,
    ftp_email: str,
    var: str,
    init_date: str,
    out_file: Path,
) -> bool:
    """Download all 4 GMAO ensemble member files, merge into SubX format.

    Output dimensions: (S: 1, M: 4, [P: N,] L: 45, Y: 181, X: 360)
    """
    import numpy as np
    import pandas as pd
    import xarray as xr

    is_multilevel = var in _GMAO_VAR_LEVELS
    var_prefix = _GMAO_VAR_PREFIX.get(var)
    if not is_multilevel and var_prefix is None:
        print(f"[DIRECT][GMAO] Variable '{var}' not available on GMAO server; skipping.")
        return False

    # GMAO uses the same date-string format as ESRL: ddmonyyyy
    gmao_date = _esrl_date_str(init_date)
    init_ts = pd.Timestamp(init_date)
    base_url = http_url.rstrip("/")

    member_arrays: List = []
    lead_days_ref: Optional[np.ndarray] = None

    def _fetch_gmao(prefix: str, member: str, tmpdir: str) -> "xr.Dataset":
        fname = f"{prefix}_GMAOGEOS_{gmao_date}_00z_d01_d45_{member}.nc"
        url = f"{base_url}/{fname}"
        tmp_file = Path(tmpdir) / fname
        print(f"[DIRECT][GMAO] Downloading {url}")
        _download_file(url, tmp_file, ftp_email)
        return _snap_grid_coords(xr.open_dataset(tmp_file))

    def _lead_days_gmao(ds: "xr.Dataset") -> "np.ndarray":
        time_vals = pd.DatetimeIndex(ds["time"].values)
        return np.array(
            [(t - init_ts).total_seconds() / 86400.0 for t in time_vals],
            dtype=np.float32,
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        for m_idx, member in enumerate(_GMAO_MEMBERS, start=1):
            try:
                if is_multilevel:
                    level_arrays: List = []
                    for p_val, prefix in _GMAO_VAR_LEVELS[var]:
                        ds = _fetch_gmao(prefix, member, tmpdir)
                        if lead_days_ref is None:
                            lead_days_ref = _lead_days_gmao(ds)
                        # Drop auxiliary bound variables and the orphan bnds dim
                        drop = [v for v in ds.data_vars if "bnds" in v.lower()]
                        ds = ds.drop_vars(drop, errors="ignore")
                        da_lev = (
                            ds[var]
                            .drop_vars(["lev", "lev_bnds"], errors="ignore")
                            .rename({"time": "L", "lat": "Y", "lon": "X"})
                            .assign_coords(L=lead_days_ref)
                        )
                        # Some GMAO per-level files carry a genuine (size-1) 'lev'
                        # axis alongside the per-level P value assigned below;
                        # dropping the coordinate label above only removes the
                        # label, not the axis itself, so squeeze it out here or
                        # every variable in the merged dataset ends up with a
                        # mismatched, spurious 'lev' dimension.
                        if "lev" in da_lev.dims:
                            da_lev = da_lev.squeeze("lev", drop=True)
                        da_lev["L"].attrs["units"] = "days"
                        da_lev = da_lev.expand_dims(P=[p_val])
                        level_arrays.append(da_lev)
                        ds.close()
                    da_member = xr.concat(level_arrays, dim="P").expand_dims(M=[m_idx])
                else:
                    ds = _fetch_gmao(var_prefix, member, tmpdir)
                    if lead_days_ref is None:
                        lead_days_ref = _lead_days_gmao(ds)
                    drop = [v for v in ds.data_vars if "bnds" in v.lower()]
                    ds = ds.drop_vars(drop, errors="ignore")
                    da_member = (
                        ds[var]
                        .rename({"time": "L", "lat": "Y", "lon": "X"})
                        .assign_coords(L=lead_days_ref)
                    )
                    da_member["L"].attrs["units"] = "days"
                    da_member = da_member.expand_dims(M=[m_idx])
                    ds.close()
            except Exception as exc:
                print(f"[DIRECT][GMAO][WARN] Failed for member {member}: {exc}", file=sys.stderr)
                return False
            member_arrays.append(da_member)

        combined = xr.concat(member_arrays, dim="M")
        combined = combined.expand_dims(S=[np.datetime64(init_ts, "ns")])
        ds_out = combined.to_dataset(name=var)

        out_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_file.with_suffix(".tmp.nc")
        ds_out.to_netcdf(tmp)
        tmp.rename(out_file)

    return True

# ── end GMAO helpers ───────────────────────────────────────────────────────────


# ── GMAO V3 (GEOS-S2S-3) -specific download helpers ───────────────────────────
# Portal (https://portal.nccs.nasa.gov/datashare/gmao/geos-s2s-3/NRT/SubC/)
# organizes files under one date subdirectory per init date, flat inside:
# File pattern: {prefix}_GMAOGEOS_{YYYYMMDD}_{member}.nc4
# Unlike V2: (1) plain YYYYMMDD, not ddmonyyyy, and no _00z_d01_d45_
# lead-window token; (2) member tokens are ensNN, not mNN; (3) ensemble size
# varies by init date -- 5 members (ens01-ens05) on regular dates, 15
# (ens01-ens15, members 6-15 are ocean-perturbed) on the last init date of
# each month -- confirmed live 2026-07-14, so members are discovered per
# date rather than hardcoded like V2's fixed _GMAO_MEMBERS list; (4) unlike
# V2's files (whose in-file data variable already matches the SubX var name,
# e.g. pr_sfc_*.nc contains "pr"), V3's files carry GMAO's raw internal GEOS
# diagnostic names -- confirmed live: pr_sfc->PRECTOTCORR, olr->OLR,
# tas_2m->T2M, ts_sfc->TS, ua_*->U, va_*->V, zg_*->H (same convention as the
# hindcast/retro portal's _VAR_RENAME in static/download_geos_v3_hindcast.py,
# just a larger set since this covers ua/va/zg too) -- so every var needs an
# explicit in-file rename, not just rlut/olr.

_GMAO_V3_VAR_PREFIX: Dict[str, str] = {
    "pr":   "pr_sfc",
    "rlut": "olr",
    "tas":  "tas_2m",
    "ts":   "ts_sfc",
}

_GMAO_V3_VAR_LEVELS: Dict[str, List[Tuple[int, str]]] = {
    "ua": [(200, "ua_200"), (850, "ua_850")],
    "va": [(200, "va_200"), (850, "va_850")],
    "zg": [(200, "zg_200"), (500, "zg_500")],
}

# SubX var -> actual in-file data variable name on the V3 portal.
_GMAO_V3_INFILE_VARNAME: Dict[str, str] = {
    "pr":   "PRECTOTCORR",
    "rlut": "OLR",
    "tas":  "T2M",
    "ts":   "TS",
    "ua":   "U",
    "va":   "V",
    "zg":   "H",
}


def _regrid_gmao_v3_to_1deg(ds: "xr.Dataset") -> "xr.Dataset":
    """Subsample V3's native 720x361 0.5-deg (-180..180) grid to 360x181
    1-deg (0..359), matching the SubX convention every other model already
    uses. Same logic as static/download_geos_v3_hindcast.py's
    _regrid_to_1deg (different portal, same native grid) -- confirmed live
    2026-07-14 that the NRT files are natively 361x720, not already on the
    181x360 grid V2's files use, so _snap_grid_coords (which only rounds to
    the nearest *integer* degree) is wrong here: it would collapse distinct
    0.5-deg-spaced points together instead of properly subsampling.

    The native grid is an exact 2:1 superset of the target grid, so no
    interpolation is needed. Longitude is snapped to the nearest 0.5 before
    the 0-360 conversion to avoid floating-point noise at lon~=0 wrapping
    that point to ~359.999 and shifting every subsequent sample by one index.
    """
    import numpy as np

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
            f"GMAO V3 regrid did not produce the expected 181x360 grid "
            f"(got lat={ds['lat'].values[:3]}..{ds['lat'].values[-3:]}, "
            f"lon={ds['lon'].values[:3]}..{ds['lon'].values[-3:]})"
        )
    # allclose above only validates closeness -- the subsampled native grid's
    # floating-point values (e.g. the equator landing at ~2.9e-13 instead of
    # exactly 0.0) were never actually replaced. Assign the exact values so
    # this model's lat/lon match other models bit-for-bit; otherwise
    # downstream multi-model merges in products/forecast.py treat GEOS_V3's
    # near-0.0 as a distinct coordinate from every other model's exact 0.0,
    # silently inflating the merged grid from 181 to 182 lat points and
    # breaking nearest-neighbor threshold alignment in compute_exceedance.
    ds = ds.assign_coords(lat=expected_lat, lon=expected_lon)
    return ds


def _list_gmao_v3_members(http_url: str, init_date: str) -> List[str]:
    """Return sorted ensNN member tokens actually present for one V3 init date.

    Member count varies (5 normally, 15 on month-end dates), so this lists
    the date directory rather than assuming a fixed member list.
    """
    date_dir_url = f"{http_url.rstrip('/')}/{init_date}/"
    try:
        with urlopen(date_dir_url, timeout=30) as resp:
            text = resp.read().decode("utf-8", errors="ignore")
    except Exception as exc:
        print(f"[DIRECT][GMAO_V3][WARN] Failed listing date dir {date_dir_url}: {exc}", file=sys.stderr)
        return []

    tokens = set(re.findall(r"_(ens\d+)\.nc4?(?:[\"'])", text, flags=re.IGNORECASE))
    return sorted(tokens, key=lambda t: int(re.search(r"\d+", t).group()))


def _list_gmao_v3_dates(http_url: str, var: str, valid_dates: Sequence[str]) -> List[str]:
    """Return available init dates for a variable on the GMAO V3 portal, newest first."""
    if var in _GMAO_V3_VAR_LEVELS:
        sentinel_prefix = _GMAO_V3_VAR_LEVELS[var][0][1].lower()
    else:
        sentinel_prefix = _GMAO_V3_VAR_PREFIX.get(var, "").lower()
    if not sentinel_prefix:
        return []
    entries = _http_list_gmao_v3(http_url, valid_dates)
    dates: set = set()
    for entry in entries:
        name = entry.name.lower()
        if not (name.startswith(sentinel_prefix + "_gmaogeos_") and name.endswith("_ens01.nc4")):
            continue
        for d in _extract_dates(entry.name):
            dates.add(d)
    return sorted(dates, reverse=True)


def _download_gmao_v3_to_subx(
    http_url: str,
    ftp_email: str,
    var: str,
    init_date: str,
    out_file: Path,
) -> bool:
    """Download all GMAO V3 ensemble member files for one init date, merge into SubX format.

    Output dimensions: (S: 1, M: 5-or-15, [P: 2,] L: N, Y: 181, X: 360)
    """
    import numpy as np
    import pandas as pd
    import xarray as xr

    is_multilevel = var in _GMAO_V3_VAR_LEVELS
    var_prefix = _GMAO_V3_VAR_PREFIX.get(var)
    if not is_multilevel and var_prefix is None:
        print(f"[DIRECT][GMAO_V3] Variable '{var}' not available on GMAO V3 portal; skipping.")
        return False

    members = _list_gmao_v3_members(http_url, init_date)
    if not members:
        print(f"[DIRECT][GMAO_V3] No members found for {init_date}; skipping.")
        return False

    init_ts = pd.Timestamp(init_date)
    base_url = f"{http_url.rstrip('/')}/{init_date}"

    member_arrays: List = []
    lead_days_ref: Optional[np.ndarray] = None

    def _fetch_gmao_v3(prefix: str, member: str, tmpdir: str) -> "xr.Dataset":
        fname = f"{prefix}_GMAOGEOS_{init_date}_{member}.nc4"
        url = f"{base_url}/{fname}"
        tmp_file = Path(tmpdir) / fname
        print(f"[DIRECT][GMAO_V3] Downloading {url}")
        _download_file(url, tmp_file, ftp_email)
        ds = _regrid_gmao_v3_to_1deg(xr.open_dataset(tmp_file))
        # V3's files carry GMAO's raw in-file diagnostic names (PRECTOTCORR,
        # T2M, OLR, TS, U, V, H), not the SubX var name the way V2's files
        # already do -- rename so downstream ds[var] works uniformly.
        infile_name = _GMAO_V3_INFILE_VARNAME.get(var)
        if infile_name and infile_name in ds.data_vars:
            ds = ds.rename({infile_name: var})
        return ds

    def _lead_days_gmao_v3(ds: "xr.Dataset") -> "np.ndarray":
        time_vals = pd.DatetimeIndex(ds["time"].values)
        return np.array(
            [(t - init_ts).total_seconds() / 86400.0 for t in time_vals],
            dtype=np.float32,
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        for m_idx, member in enumerate(members, start=1):
            try:
                if is_multilevel:
                    level_arrays: List = []
                    for p_val, prefix in _GMAO_V3_VAR_LEVELS[var]:
                        ds = _fetch_gmao_v3(prefix, member, tmpdir)
                        if lead_days_ref is None:
                            lead_days_ref = _lead_days_gmao_v3(ds)
                        drop = [v for v in ds.data_vars if "bnds" in v.lower()]
                        ds = ds.drop_vars(drop, errors="ignore")
                        da_lev = (
                            ds[var]
                            .drop_vars(["lev", "lev_bnds"], errors="ignore")
                            .rename({"time": "L", "lat": "Y", "lon": "X"})
                            .assign_coords(L=lead_days_ref)
                        )
                        if "lev" in da_lev.dims:
                            da_lev = da_lev.squeeze("lev", drop=True)
                        da_lev["L"].attrs["units"] = "days"
                        da_lev = da_lev.expand_dims(P=[p_val])
                        level_arrays.append(da_lev)
                        ds.close()
                    da_member = xr.concat(level_arrays, dim="P").expand_dims(M=[m_idx])
                else:
                    ds = _fetch_gmao_v3(var_prefix, member, tmpdir)
                    if lead_days_ref is None:
                        lead_days_ref = _lead_days_gmao_v3(ds)
                    drop = [v for v in ds.data_vars if "bnds" in v.lower()]
                    ds = ds.drop_vars(drop, errors="ignore")
                    da_member = (
                        ds[var]
                        .rename({"time": "L", "lat": "Y", "lon": "X"})
                        .assign_coords(L=lead_days_ref)
                    )
                    da_member["L"].attrs["units"] = "days"
                    da_member = da_member.expand_dims(M=[m_idx])
                    ds.close()
            except Exception as exc:
                print(f"[DIRECT][GMAO_V3][WARN] Failed for member {member}: {exc}", file=sys.stderr)
                return False
            member_arrays.append(da_member)

        combined = xr.concat(member_arrays, dim="M")
        combined = combined.expand_dims(S=[np.datetime64(init_ts, "ns")])
        ds_out = combined.to_dataset(name=var)

        out_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_file.with_suffix(".tmp.nc")
        ds_out.to_netcdf(tmp)
        tmp.rename(out_file)

    return True

# ── end GMAO V3 helpers ────────────────────────────────────────────────────────


# ── CFS-specific download helpers ─────────────────────────────────────────────
# CPC mirror (https://ftp.cpc.ncep.noaa.gov/dcollins/SubX/CFS/) organizes files
# under one subdirectory per variable/level, e.g. pr_sfc/realtime/, zg_500/realtime/.
# File pattern: {prefix}_CFS_{ddmonyyyy}_{HH}z_d00_d44_{member}.nc
# CPC publishes 4 init cycles/day (00z/06z/12z/18z), each with 4 members; all 4
# cycles are fetched and stacked along S so the daily SubX archive file matches
# the historical convention of S:4/day (one real per-cycle initialization each,
# not just the 00z cycle).
# Each file has exactly one data variable, whose in-file name is looked up
# dynamically rather than hardcoded (GEFS/CFS use inconsistent internal names).

_CFS_VAR_PREFIX: Dict[str, str] = {
    "pr":  "pr_sfc",
    "tas": "tas_2m",
    "ua":  "ua_200",
    "va":  "va_200",
    "ts":  "ts",
}

_CFS_VAR_LEVELS: Dict[str, List[Tuple[int, str]]] = {
    "zg": [(500, "zg_500"), (200, "zg_200")],
}

_CFS_MEMBERS = ["m01", "m02", "m03", "m04"]
_CFS_HOURS = ["00z", "06z", "12z", "18z"]


def _list_cfs_dates(base_url: str, var: str) -> List[str]:
    """Return available 00z init dates for a variable on the CFS mirror, newest first."""
    if var in _CFS_VAR_LEVELS:
        prefix = _CFS_VAR_LEVELS[var][0][1]
    else:
        prefix = _CFS_VAR_PREFIX.get(var, "")
    if not prefix:
        return []
    list_url = f"{base_url.rstrip('/')}/{prefix}/realtime/"
    entries = _http_list(list_url, depth=0)
    dates: set = set()
    for entry in entries:
        name = entry.name.lower()
        if not (name.startswith(prefix.lower() + "_cfs_") and "_00z_" in name and name.endswith("_m01.nc")):
            continue
        for d in _extract_dates(entry.name):
            dates.add(d)
    return sorted(dates, reverse=True)


def _download_cfs_to_subx(
    base_url: str,
    ftp_email: str,
    var: str,
    init_date: str,
    out_file: Path,
) -> bool:
    """Download all 4 CFS cycles (00z/06z/12z/18z), each with 4 ensemble members,
    merge into SubX format.

    Output dimensions: (S: 4, M: 4, [P: N,] L: 44, Y: 181, X: 360)
    """
    import numpy as np
    import pandas as pd
    import xarray as xr

    is_multilevel = var in _CFS_VAR_LEVELS
    var_prefix = _CFS_VAR_PREFIX.get(var)
    if not is_multilevel and var_prefix is None:
        print(f"[DIRECT][CFS] Variable '{var}' not available on CFS mirror; skipping.")
        return False

    cfs_date = _esrl_date_str(init_date)
    init_ts = pd.Timestamp(init_date)
    base = base_url.rstrip("/")

    def _fetch_cfs(prefix: str, member: str, hour: str, tmpdir: str) -> "xr.Dataset":
        fname = f"{prefix}_CFS_{cfs_date}_{hour}_d00_d44_{member}.nc"
        url = f"{base}/{prefix}/realtime/{fname}"
        tmp_file = Path(tmpdir) / fname
        print(f"[DIRECT][CFS] Downloading {url}")
        _download_file(url, tmp_file, ftp_email)
        return _snap_grid_coords(xr.open_dataset(tmp_file, decode_times=False))

    def _lead_days_cfs(ds: "xr.Dataset") -> "np.ndarray":
        # time units are "days since {init_date} 00:00:00"; values are already lead days.
        return ds["time"].values.astype(np.float32)

    def _data_var(ds: "xr.Dataset") -> str:
        return list(ds.data_vars)[0]

    # hour_slots holds (hour, combined_hour-or-None); None means every member for
    # that cycle failed to download and must be NaN-padded in a second pass below
    # once we know the correct shape from some other cycle that did succeed.
    hour_slots: List[Tuple[str, Optional["xr.DataArray"]]] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        for hour in _CFS_HOURS:
            # member_slots[i] is None until member i+1 downloads successfully; a
            # missing member (e.g. one 404 among the 4 hours x 4 members x N
            # levels this fetch needs) no longer aborts the whole date -- it's
            # NaN-padded from a sibling member's shape instead, so the other
            # successfully-downloaded members/cycles aren't thrown away too.
            member_slots: List[Optional["xr.DataArray"]] = [None] * len(_CFS_MEMBERS)
            lead_days_ref: Optional[np.ndarray] = None
            for m_idx, member in enumerate(_CFS_MEMBERS, start=1):
                try:
                    if is_multilevel:
                        level_arrays: List = []
                        for p_val, prefix in _CFS_VAR_LEVELS[var]:
                            ds = _fetch_cfs(prefix, member, hour, tmpdir)
                            if lead_days_ref is None:
                                lead_days_ref = _lead_days_cfs(ds)
                            da_lev = (
                                ds[_data_var(ds)]
                                .rename({"time": "L", "lat": "Y", "lon": "X"})
                                .assign_coords(L=lead_days_ref)
                            )
                            da_lev["L"].attrs["units"] = "days"
                            da_lev = da_lev.expand_dims(P=[p_val])
                            level_arrays.append(da_lev)
                            ds.close()
                        da_member = xr.concat(level_arrays, dim="P").expand_dims(M=[m_idx])
                    else:
                        ds = _fetch_cfs(var_prefix, member, hour, tmpdir)
                        if lead_days_ref is None:
                            lead_days_ref = _lead_days_cfs(ds)
                        da_member = (
                            ds[_data_var(ds)]
                            .rename({"time": "L", "lat": "Y", "lon": "X"})
                            .assign_coords(L=lead_days_ref)
                        )
                        da_member["L"].attrs["units"] = "days"
                        da_member = da_member.expand_dims(M=[m_idx])
                        ds.close()
                except Exception as exc:
                    print(f"[DIRECT][CFS][WARN] Failed for {hour} member {member}: {exc}; NaN-padding this member", file=sys.stderr)
                    continue
                member_slots[m_idx - 1] = da_member

            if all(m is None for m in member_slots):
                print(f"[DIRECT][CFS][WARN] All members failed for {hour}; NaN-padding entire cycle", file=sys.stderr)
                hour_slots.append((hour, None))
                continue

            template = next(m for m in member_slots if m is not None)
            resolved_members = [
                da if da is not None else xr.full_like(template, np.nan).assign_coords(M=[m_idx])
                for m_idx, da in enumerate(member_slots, start=1)
            ]
            combined_hour = xr.concat(resolved_members, dim="M")
            hour_offset = pd.Timedelta(hours=int(hour.rstrip("z")))
            combined_hour = combined_hour.expand_dims(S=[np.datetime64(init_ts + hour_offset, "ns")])
            hour_slots.append((hour, combined_hour))

        if all(arr is None for _, arr in hour_slots):
            print(f"[DIRECT][CFS][ERROR] All cycles failed for var={var} date={init_date}; nothing to write.", file=sys.stderr)
            return False

        hour_template = next(arr for _, arr in hour_slots if arr is not None)
        hour_arrays: List = []
        for hour, arr in hour_slots:
            if arr is None:
                hour_offset = pd.Timedelta(hours=int(hour.rstrip("z")))
                nan_hour = xr.full_like(hour_template, np.nan)
                nan_hour = nan_hour.assign_coords(S=[np.datetime64(init_ts + hour_offset, "ns")])
                hour_arrays.append(nan_hour)
            else:
                hour_arrays.append(arr)

        combined = xr.concat(hour_arrays, dim="S")
        # Match the existing raw-ingest convention (P first when present).
        dim_order = ("P", "S", "M", "L", "Y", "X") if "P" in combined.dims else ("S", "M", "L", "Y", "X")
        combined = combined.transpose(*[d for d in dim_order if d in combined.dims])
        ds_out = combined.to_dataset(name=var)

        out_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_file.with_suffix(".tmp.nc")
        ds_out.to_netcdf(tmp)
        tmp.rename(out_file)

    return True

# ── end CFS helpers ────────────────────────────────────────────────────────────


# ── GEFS-specific download helpers ────────────────────────────────────────────
# CPC mirror (https://ftp.cpc.ncep.noaa.gov/dcollins/SubX/GEFS/) keeps all
# variables in one flat directory. File pattern:
#   {prefix}_GEFS_{ddmonyyyy}_00z_d01_d35_{member}.nc
# 31 members (m00-m30), single 00z cycle/day. Each file has exactly one data
# variable, whose in-file name is looked up dynamically (e.g. PRATE_P1_L1_GLL0).

_GEFS_VAR_PREFIX: Dict[str, str] = {
    "pr":  "prate_sfc",
    "tas": "tas_2m",
}

_GEFS_VAR_LEVELS: Dict[str, List[Tuple[int, str]]] = {
    "zg": [(500, "zg_500")],
}

_GEFS_MEMBERS = ["m%02d" % i for i in range(31)]


def _list_gefs_dates(base_url: str, var: str) -> List[str]:
    """Return available 00z init dates for a variable on the GEFS mirror, newest first."""
    if var in _GEFS_VAR_LEVELS:
        prefix = _GEFS_VAR_LEVELS[var][0][1]
    else:
        prefix = _GEFS_VAR_PREFIX.get(var, "")
    if not prefix:
        return []
    entries = _http_list(base_url, depth=0)
    dates: set = set()
    for entry in entries:
        name = entry.name.lower()
        if not (name.startswith(prefix.lower() + "_gefs_") and "_00z_" in name and name.endswith("_m00.nc")):
            continue
        for d in _extract_dates(entry.name):
            dates.add(d)
    return sorted(dates, reverse=True)


def _download_gefs_to_subx(
    base_url: str,
    ftp_email: str,
    var: str,
    init_date: str,
    out_file: Path,
) -> bool:
    """Download all 31 GEFS ensemble member files (00z cycle), merge into SubX format.

    Output dimensions: (S: 1, M: 31, [P: 1,] L: 34, Y: 181, X: 360)
    """
    import numpy as np
    import pandas as pd
    import xarray as xr

    is_multilevel = var in _GEFS_VAR_LEVELS
    var_prefix = _GEFS_VAR_PREFIX.get(var)
    if not is_multilevel and var_prefix is None:
        print(f"[DIRECT][GEFS] Variable '{var}' not available on GEFS mirror; skipping.")
        return False

    gefs_date = _esrl_date_str(init_date)
    init_ts = pd.Timestamp(init_date)
    base = base_url.rstrip("/")

    # CPC's GEFS files carry 35 lead days; the legacy IRIDL-sourced archive (and
    # the precomputed climatology it's compared against in products/forecast.py)
    # only covers 34. Truncate to stay compatible until the climatology is
    # regenerated to cover the extra day.
    max_lead_days = 34

    member_arrays: List = []
    lead_days_ref: Optional[np.ndarray] = None

    def _fetch_gefs(prefix: str, member: str, tmpdir: str) -> "xr.Dataset":
        fname = f"{prefix}_GEFS_{gefs_date}_00z_d01_d35_{member}.nc"
        url = f"{base}/{fname}"
        tmp_file = Path(tmpdir) / fname
        print(f"[DIRECT][GEFS] Downloading {url}")
        _download_file(url, tmp_file, ftp_email)
        ds = _snap_grid_coords(xr.open_dataset(tmp_file, decode_times=False))
        return ds.isel(time=slice(0, max_lead_days))

    def _lead_days_gefs(ds: "xr.Dataset") -> "np.ndarray":
        return ds["time"].values.astype(np.float32)

    def _data_var(ds: "xr.Dataset") -> str:
        return list(ds.data_vars)[0]

    with tempfile.TemporaryDirectory() as tmpdir:
        for m_idx, member in enumerate(_GEFS_MEMBERS):
            try:
                if is_multilevel:
                    level_arrays: List = []
                    for p_val, prefix in _GEFS_VAR_LEVELS[var]:
                        ds = _fetch_gefs(prefix, member, tmpdir)
                        if lead_days_ref is None:
                            lead_days_ref = _lead_days_gefs(ds)
                        da_lev = (
                            ds[_data_var(ds)]
                            .rename({"time": "L", "lat": "Y", "lon": "X"})
                            .assign_coords(L=lead_days_ref)
                        )
                        da_lev["L"].attrs["units"] = "days"
                        da_lev = da_lev.expand_dims(P=[p_val])
                        level_arrays.append(da_lev)
                        ds.close()
                    da_member = xr.concat(level_arrays, dim="P").expand_dims(M=[m_idx])
                else:
                    ds = _fetch_gefs(var_prefix, member, tmpdir)
                    if lead_days_ref is None:
                        lead_days_ref = _lead_days_gefs(ds)
                    da_member = (
                        ds[_data_var(ds)]
                        .rename({"time": "L", "lat": "Y", "lon": "X"})
                        .assign_coords(L=lead_days_ref)
                    )
                    da_member["L"].attrs["units"] = "days"
                    da_member = da_member.expand_dims(M=[m_idx])
                    ds.close()
            except Exception as exc:
                print(f"[DIRECT][GEFS][WARN] Failed for member {member}: {exc}", file=sys.stderr)
                return False
            member_arrays.append(da_member)

        combined = xr.concat(member_arrays, dim="M")
        combined = combined.expand_dims(S=[np.datetime64(init_ts, "ns")])
        # Match the existing raw-ingest convention (P first when present).
        dim_order = ("P", "S", "M", "L", "Y", "X") if "P" in combined.dims else ("S", "M", "L", "Y", "X")
        combined = combined.transpose(*[d for d in dim_order if d in combined.dims])
        ds_out = combined.to_dataset(name=var)

        out_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_file.with_suffix(".tmp.nc")
        ds_out.to_netcdf(tmp)
        tmp.rename(out_file)

    return True

# ── end GEFS helpers ───────────────────────────────────────────────────────────


# ── ECCC conversion helper ─────────────────────────────────────────────────────
# ECCC provides one tar per member (m00-m20 = 21 members) per init date.
# Each tar contains all variables; init date is in the time coordinate's
# reference_date attribute as "YYYY.MM.DD HH:MM:SS UTC".

_ECCC_ALL_MEMBERS = [f"m{i:02d}" for i in range(21)]


def _download_eccc_members_to_subx(
    http_url: str,
    ftp_email: str,
    var: str,
    init_date: str,
    out_file: Path,
    eccc_members: List[str],
    preferred_level: Optional[int] = None,
) -> bool:
    """Download all ECCC member tars, extract var, convert and merge into SubX format.

    Output dimensions: (S: 1, M: N, L: 39, Y: 181, X: 360)
    preferred_level: if set, selects the per-level file from the tar matching
    {var}_{preferred_level}_* (e.g. ua_200_ECCC_...) instead of alphabetical first.
    """
    import numpy as np
    import pandas as pd
    import xarray as xr

    init_ts = pd.Timestamp(init_date)
    date_token = f"{init_date}00"   # YYYYMMDD → YYYYMMDDH (ECCC uses 00z suffix)

    entries = _http_list_eccc(http_url, [init_date])
    tar_map = {e.name: e for e in entries if e.name.endswith(".tar")}

    if not tar_map:
        print(f"[DIRECT][ECCC] No tar files found for {init_date} at {http_url}")
        return False

    member_arrays: List = []
    lead_days_ref: Optional[np.ndarray] = None

    with tempfile.TemporaryDirectory() as tmpdir:
        for m_idx, member in enumerate(eccc_members, start=1):
            tar_name = f"subX_realtime_ECCC_{date_token}_{member}.tar"
            entry = tar_map.get(tar_name)
            if entry is None:
                print(f"[DIRECT][ECCC][WARN] Tar not found: {tar_name}")
                continue

            tar_tmp = Path(tmpdir) / tar_name
            nc_tmp = Path(tmpdir) / f"{var}_{member}.nc"

            print(f"[DIRECT][ECCC] Downloading {entry.url}")
            try:
                _download_file(entry.url, tar_tmp, ftp_email)
                ok = _extract_from_tar(tar_tmp, nc_tmp, var, [member], preferred_level=preferred_level)
                if not ok:
                    print(f"[DIRECT][ECCC][WARN] '{var}' not in {tar_name}")
                    continue
            except Exception as exc:
                print(f"[DIRECT][ECCC][WARN] Failed {tar_name}: {exc}", file=sys.stderr)
                continue

            ds = xr.open_dataset(nc_tmp)

            # Init date from reference_date attr (format "2026.06.04 00:00:00 UTC")
            ref_str = ds["time"].attrs.get("reference_date", "")
            ref_ts = pd.Timestamp(ref_str.replace(" UTC", "")) if ref_str else init_ts

            time_vals = pd.DatetimeIndex(ds["time"].values)
            lead_days = np.array(
                [(t - ref_ts).total_seconds() / 86400.0 for t in time_vals],
                dtype=np.float32,
            )
            if lead_days_ref is None:
                lead_days_ref = lead_days

            da_member = (
                ds[var]
                .rename({"time": "L", "latitude": "Y", "longitude": "X"})
                .assign_coords(L=lead_days_ref)
            )
            da_member["L"].attrs["units"] = "days"
            da_member = da_member.expand_dims(M=[m_idx])
            member_arrays.append(da_member)
            ds.close()

    if not member_arrays:
        print(f"[DIRECT][ECCC] No members extracted for {var} {init_date}")
        return False

    import numpy as np
    import pandas as pd
    import xarray as xr

    combined = xr.concat(member_arrays, dim="M")
    if preferred_level is not None:
        # A single per-level file was selected above (var_levels config), so the
        # data itself only ever has one pressure level -- but the archive's
        # existing schema for this variable has a real P dimension from earlier
        # history. Embed a size-1 P dim at the selected level so this merges
        # against the archive's P axis instead of silently losing the P
        # dimension (which previously looked like "format changed, no P dim").
        combined = combined.expand_dims(P=[preferred_level])
    combined = combined.expand_dims(S=[np.datetime64(init_ts, "ns")])
    ds_out = combined.to_dataset(name=var)

    out_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_file.with_suffix(".tmp.nc")
    ds_out.to_netcdf(tmp)
    tmp.rename(out_file)
    return True

# ── end ECCC helpers ───────────────────────────────────────────────────────────


# ── RSMAS-specific download helpers ───────────────────────────────────────────
# RSMAS publishes 9 ensemble members (m01-m09) per date, one file per
# variable/level, e.g. pr_sfc_CCSM4_28jun2026_00z_d01_d45_m01.nc. Files use
# uppercase dim/var names (TIME, LAT, LON, PR) and a cftime NoLeap calendar.

_RSMAS_VAR_PREFIX: Dict[str, str] = {
    "pr":   "pr_sfc",
    "rlut": "rlut_toa",
    "tas":  "tas_2m",
    "ts":   "ts_sfc",
}

_RSMAS_MEMBERS = ["m%02d" % i for i in range(1, 10)]


def _rsmas_prefix(var: str, var_levels: Dict[str, int]) -> Optional[str]:
    """Resolve the RSMAS filename prefix for a variable.

    ua/va/zg are published as separate single-level files (ua_200_*,
    ua_850_*, ...); the configured level (ingest.direct.rsmas.var_levels)
    picks which one. Other variables are single-level with a fixed prefix.
    """
    if var in var_levels:
        return f"{var}_{var_levels[var]}"
    return _RSMAS_VAR_PREFIX.get(var)


def _list_rsmas_dates(ftp_url: str, ftp_email: str, var: str, var_levels: Dict[str, int]) -> List[str]:
    """Return available init dates for a variable on the RSMAS FTP, newest first."""
    prefix = _rsmas_prefix(var, var_levels)
    if not prefix:
        return []
    entries = _ftp_list(ftp_url, ftp_email)
    dates: set = set()
    for entry in entries:
        name = entry.name.lower()
        if not (name.startswith(prefix.lower() + "_ccsm4_") and name.endswith("_m01.nc")):
            continue
        for d in _extract_dates(entry.name):
            dates.add(d)
    return sorted(dates, reverse=True)


def _download_rsmas_to_subx(
    ftp_url: str,
    ftp_email: str,
    var: str,
    init_date: str,
    out_file: Path,
    var_levels: Dict[str, int],
) -> bool:
    """Download all 9 RSMAS ensemble member files, merge into SubX format.

    Output dimensions: (S: 1, M: 9, [P: 1,] L: 45, Y: 181, X: 360)
    """
    import numpy as np
    import pandas as pd
    import xarray as xr

    prefix = _rsmas_prefix(var, var_levels)
    if not prefix:
        print(f"[DIRECT][RSMAS] Variable '{var}' not available on RSMAS FTP; skipping.")
        return False

    rsmas_date = _esrl_date_str(init_date)
    init_ts = pd.Timestamp(init_date)
    parsed = urlparse(ftp_url)
    base_path = parsed.path.rstrip("/")

    member_arrays: List = []
    lead_days_ref: Optional[np.ndarray] = None

    def _fetch_member(member: str, tmpdir: str) -> "xr.Dataset":
        fname = f"{prefix}_CCSM4_{rsmas_date}_00z_d01_d45_{member}.nc"
        remote_path = f"{base_path}/{fname}"
        tmp_file = Path(tmpdir) / fname
        print(f"[DIRECT][RSMAS] Downloading ftp://{parsed.hostname}{remote_path}")
        ftp = FTP(parsed.hostname, timeout=60)
        try:
            _ftp_login_with_fallback(ftp, ftp_email)
            with open(tmp_file, "wb") as f:
                ftp.retrbinary(f"RETR {remote_path}", f.write)
        except Exception as exc:
            raise RuntimeError(f"FTP download failed for {fname}: {exc}") from exc
        finally:
            try:
                ftp.quit()
            except Exception:
                pass
        # RSMAS uses a cftime NoLeap calendar; decode normally (unlike ESRL/CFS/
        # GEFS, which need decode_times=False) then convert to plain timestamps.
        return _snap_grid_coords(xr.open_dataset(tmp_file))

    def _lead_days_rsmas(ds: "xr.Dataset") -> "np.ndarray":
        time_vals = ds["TIME"].values
        if hasattr(time_vals[0], "year"):
            valid_times = [
                pd.Timestamp(t.year, t.month, t.day, t.hour, t.minute, t.second)
                for t in time_vals
            ]
        else:
            valid_times = list(pd.DatetimeIndex(time_vals))
        return np.array(
            [(v - init_ts).total_seconds() / 86400.0 for v in valid_times],
            dtype=np.float32,
        )

    def _data_var(ds: "xr.Dataset") -> str:
        candidates = [v for v in ds.data_vars if "bnd" not in v.lower() and "bound" not in v.lower()]
        return candidates[0]

    with tempfile.TemporaryDirectory() as tmpdir:
        for m_idx, member in enumerate(_RSMAS_MEMBERS, start=1):
            try:
                ds = _fetch_member(member, tmpdir)
                if lead_days_ref is None:
                    lead_days_ref = _lead_days_rsmas(ds)
                drop = [v for v in ds.data_vars if "bnd" in v.lower() or "bound" in v.lower()]
                ds = ds.drop_vars(drop, errors="ignore")
                rename_map = {"TIME": "L", "LAT": "Y", "LON": "X"}
                lev_name = next((c for c in ["lev_p", "lev", "plev"] if c in ds.coords), None)
                if lev_name:
                    rename_map[lev_name] = "P"
                da_member = ds[_data_var(ds)].rename(rename_map).assign_coords(L=lead_days_ref)
                da_member["L"].attrs["units"] = "days"
                if "P" in da_member.coords:
                    da_member = da_member.assign_coords(P=da_member["P"].values.astype(int))
                da_member = da_member.expand_dims(M=[m_idx])
                ds.close()
            except Exception as exc:
                print(f"[DIRECT][RSMAS][WARN] Failed for member {member}: {exc}", file=sys.stderr)
                return False
            member_arrays.append(da_member)

        combined = xr.concat(member_arrays, dim="M")
        combined = combined.expand_dims(S=[np.datetime64(init_ts, "ns")])
        dim_order = ("P", "S", "M", "L", "Y", "X") if "P" in combined.dims else ("S", "M", "L", "Y", "X")
        combined = combined.transpose(*[d for d in dim_order if d in combined.dims])
        ds_out = combined.to_dataset(name=var)

        out_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_file.with_suffix(".tmp.nc")
        ds_out.to_netcdf(tmp)
        tmp.rename(out_file)

    return True

# ── end RSMAS multi-member helpers ────────────────────────────────────────────


def _provider_from_source(source: str, group: str, server_model: str) -> str:
    source_l = source.strip().lower()
    if source_l.startswith("direct_"):
        return source_l.replace("direct_", "", 1)
    if source_l == "direct":
        key = f"{group}-{server_model}"
        if key == "ESRL-FIMr1p1":
            return "esrl"
        if key == "GMAO-GEOS_V2p1_5daily":
            return "gmao"
        if key == "GMAO-GEOS_V3":
            return "gmao_v3"
        if key == "ECCC-GEPS8":
            return "eccc"
        if key == "RSMAS-CCSM4":
            return "rsmas"
    return source_l


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Direct SubX provider downloader")
    parser.add_argument("--source", required=True)
    parser.add_argument("--group", required=True)
    parser.add_argument("--server-model", required=True)
    parser.add_argument("--local-model", required=True)
    parser.add_argument("--var", required=True)
    parser.add_argument("--fcst", required=True)
    parser.add_argument("--target-dir", required=True)
    parser.add_argument("--config", default="")
    args = parser.parse_args(argv)

    provider = _provider_from_source(args.source, args.group, args.server_model)
    if provider not in DEFAULT_PROVIDER_URLS:
        print(f"[DIRECT] Unsupported source/provider: {args.source}", file=sys.stderr)
        return 2

    cfg = _load_cfg(args.config)
    lookback_days = _cfg_lookback_days(cfg)
    date_window = _lookback_dates(args.fcst, lookback_days)
    ftp_email = _ftp_email(cfg)
    eccc_members = _cfg_eccc_members(cfg)
    eccc_var_levels = _cfg_eccc_var_levels(cfg)
    rsmas_var_levels = _cfg_provider_var_levels(cfg, "rsmas")
    provider_url = _cfg_provider_url(cfg, provider)

    print(
        f"[DIRECT] provider={provider} url={provider_url} var={args.var} "
        f"fcst={args.fcst} lookback_days={lookback_days}"
    )

    target_dir = Path(args.target_dir)
    downloaded = 0

    # ── Per-member download+merge paths ───────────────────────────────────────
    if provider == "gmao":
        available = _list_gmao_dates(provider_url, args.var)
        if not available:
            print(f"[DIRECT][GMAO] No files found for var={args.var} at {provider_url}")
            return 0
        date_window_set = set(date_window)
        matching = [d for d in available if d in date_window_set]
        if not matching:
            print(
                f"[DIRECT][GMAO] No files in lookback window for var={args.var} "
                f"(window={date_window[-1]}..{date_window[0]})"
            )
            return 0
        for init_date in matching:
            out_file = target_dir / f"{args.var}_{args.group}-{args.local_model}_{init_date}.daily.nc"
            if out_file.exists():
                print(f"[DIRECT] Exists, skipping: {out_file}")
                continue
            ok = _download_gmao_to_subx(provider_url, ftp_email, args.var, init_date, out_file)
            if ok:
                downloaded += 1
        print(f"[DIRECT] Completed provider={provider} downloaded={downloaded}")
        return 0

    if provider == "gmao_v3":
        available = _list_gmao_v3_dates(provider_url, args.var, date_window)
        if not available:
            print(f"[DIRECT][GMAO_V3] No files found for var={args.var} at {provider_url}")
            return 0
        date_window_set = set(date_window)
        matching = [d for d in available if d in date_window_set]
        if not matching:
            print(
                f"[DIRECT][GMAO_V3] No files in lookback window for var={args.var} "
                f"(window={date_window[-1]}..{date_window[0]})"
            )
            return 0
        for init_date in matching:
            out_file = target_dir / f"{args.var}_{args.group}-{args.local_model}_{init_date}.daily.nc"
            if out_file.exists():
                print(f"[DIRECT] Exists, skipping: {out_file}")
                continue
            ok = _download_gmao_v3_to_subx(provider_url, ftp_email, args.var, init_date, out_file)
            if ok:
                downloaded += 1
        print(f"[DIRECT] Completed provider={provider} downloaded={downloaded}")
        return 0

    if provider == "eccc":
        entries = _list_entries(provider_url, ftp_email, provider, date_window)
        if not entries:
            print(f"[DIRECT][WARN] No entries discovered at provider URL: {provider_url}")
            return 0
        dates_with_tars = sorted(
            {d for e in entries if e.name.endswith(".tar")
             for d in _extract_dates(e.name)},
            reverse=True,
        )
        date_window_set = set(date_window)
        matching = [d for d in dates_with_tars if d in date_window_set]
        if not matching:
            print(f"[DIRECT][ECCC] No tar files in lookback window for {args.var}")
            return 0
        for init_date in matching:
            out_file = target_dir / f"{args.var}_{args.group}-{args.local_model}_{init_date}.daily.nc"
            if out_file.exists():
                print(f"[DIRECT] Exists, skipping: {out_file}")
                continue
            ok = _download_eccc_members_to_subx(
                provider_url, ftp_email, args.var, init_date, out_file, eccc_members,
                preferred_level=eccc_var_levels.get(args.var),
            )
            if ok:
                downloaded += 1
        print(f"[DIRECT] Completed provider={provider} downloaded={downloaded}")
        return 0

    if provider == "esrl":
        available = _list_esrl_dates(provider_url, ftp_email, args.var)
        if not available:
            print(f"[DIRECT][ESRL] No files found for var={args.var} at {provider_url}")
            return 0
        date_window_set = set(date_window)
        matching = [d for d in available if d in date_window_set]
        if not matching:
            print(
                f"[DIRECT][ESRL] No files in lookback window for var={args.var} "
                f"(window={date_window[-1]}..{date_window[0]})"
            )
            return 0
        for init_date in matching:
            out_file = target_dir / f"{args.var}_{args.group}-{args.local_model}_{init_date}.daily.nc"
            if out_file.exists():
                print(f"[DIRECT] Exists, skipping: {out_file}")
                continue
            ok = _download_esrl_to_subx(provider_url, ftp_email, args.var, init_date, out_file)
            if ok:
                downloaded += 1
        print(f"[DIRECT] Completed provider={provider} downloaded={downloaded}")
        return 0
    # ── end ESRL ───────────────────────────────────────────────────────────────

    if provider == "cfs":
        available = _list_cfs_dates(provider_url, args.var)
        if not available:
            print(f"[DIRECT][CFS] No files found for var={args.var} at {provider_url}")
            return 0
        date_window_set = set(date_window)
        matching = [d for d in available if d in date_window_set]
        if not matching:
            print(
                f"[DIRECT][CFS] No files in lookback window for var={args.var} "
                f"(window={date_window[-1]}..{date_window[0]})"
            )
            return 0
        for init_date in matching:
            out_file = target_dir / f"{args.var}_{args.group}-{args.local_model}_{init_date}.daily.nc"
            if out_file.exists():
                print(f"[DIRECT] Exists, skipping: {out_file}")
                continue
            ok = _download_cfs_to_subx(provider_url, ftp_email, args.var, init_date, out_file)
            if ok:
                downloaded += 1
        print(f"[DIRECT] Completed provider={provider} downloaded={downloaded}")
        return 0
    # ── end CFS ────────────────────────────────────────────────────────────────

    if provider == "gefs":
        available = _list_gefs_dates(provider_url, args.var)
        if not available:
            print(f"[DIRECT][GEFS] No files found for var={args.var} at {provider_url}")
            return 0
        date_window_set = set(date_window)
        matching = [d for d in available if d in date_window_set]
        if not matching:
            print(
                f"[DIRECT][GEFS] No files in lookback window for var={args.var} "
                f"(window={date_window[-1]}..{date_window[0]})"
            )
            return 0
        for init_date in matching:
            out_file = target_dir / f"{args.var}_{args.group}-{args.local_model}_{init_date}.daily.nc"
            if out_file.exists():
                print(f"[DIRECT] Exists, skipping: {out_file}")
                continue
            ok = _download_gefs_to_subx(provider_url, ftp_email, args.var, init_date, out_file)
            if ok:
                downloaded += 1
        print(f"[DIRECT] Completed provider={provider} downloaded={downloaded}")
        return 0
    # ── end GEFS ───────────────────────────────────────────────────────────────

    if provider == "rsmas":
        available = _list_rsmas_dates(provider_url, ftp_email, args.var, rsmas_var_levels)
        if not available:
            print(f"[DIRECT][RSMAS] No files found for var={args.var} at {provider_url}")
            return 0
        date_window_set = set(date_window)
        matching = [d for d in available if d in date_window_set]
        if not matching:
            print(
                f"[DIRECT][RSMAS] No files in lookback window for var={args.var} "
                f"(window={date_window[-1]}..{date_window[0]})"
            )
            return 0
        for init_date in matching:
            out_file = target_dir / f"{args.var}_{args.group}-{args.local_model}_{init_date}.daily.nc"
            if out_file.exists():
                print(f"[DIRECT] Exists, skipping: {out_file}")
                continue
            ok = _download_rsmas_to_subx(provider_url, ftp_email, args.var, init_date, out_file, rsmas_var_levels)
            if ok:
                downloaded += 1
        print(f"[DIRECT] Completed provider={provider} downloaded={downloaded}")
        return 0
    # ── end RSMAS ──────────────────────────────────────────────────────────────

    entries = _list_entries(provider_url, ftp_email, provider, date_window)
    if not entries:
        print(f"[DIRECT][WARN] No entries discovered at provider URL: {provider_url}")
        return 0

    # Some providers publish separate single-level files per variable (e.g.
    # RSMAS's ua_200_* vs ua_850_*) without a level token in the var name
    # itself; _var_regex would match either indiscriminately, so narrow to
    # the configured level first when one is set.
    provider_var_levels = _cfg_provider_var_levels(cfg, provider)
    preferred_var_level = provider_var_levels.get(args.var)
    if preferred_var_level is not None:
        level_token = f"{args.var}_{preferred_var_level}_"
        level_entries = [e for e in entries if e.name.lower().startswith(level_token.lower())]
        if level_entries:
            entries = level_entries
        else:
            print(
                f"[DIRECT][WARN] No files matching level {preferred_var_level} for var={args.var}; "
                f"falling back to unfiltered candidates"
            )

    candidates = _filter_candidates(entries, args.var, date_window, provider)
    if not candidates:
        print(
            f"[DIRECT] No matching files found for var={args.var} dates={date_window[-1]}..{date_window[0]} "
            f"from {provider_url}"
        )
        return 0

    seen_dates = set()
    for candidate in candidates:
        init_date = candidate.init_date
        entry = candidate.entry
        # Keep the newest candidate per date.
        if init_date in seen_dates:
            continue
        seen_dates.add(init_date)

        out_file = target_dir / f"{args.var}_{args.group}-{args.local_model}_{init_date}.daily.nc"
        if out_file.exists():
            print(f"[DIRECT] Exists, skipping: {out_file}")
            continue

        if candidate.from_tar:
            member_label = eccc_members[0] if len(eccc_members) == 1 else (eccc_members[0] + ".." + eccc_members[-1])
            print(
                f"[DIRECT] Downloading archive {entry.url} -> {out_file} "
                f"(member preference={member_label})"
            )
        else:
            print(f"[DIRECT] Downloading {entry.url} -> {out_file}")
        try:
            ok = _materialize_candidate(candidate, out_file, ftp_email, args.var, eccc_members)
            if ok:
                downloaded += 1
            else:
                print(
                    f"[DIRECT][WARN] No matching {args.var}/{','.join(eccc_members)} member inside archive {entry.url}",
                    file=sys.stderr,
                )
        except Exception as exc:
            print(f"[DIRECT][WARN] Failed download {entry.url}: {exc}", file=sys.stderr)

    print(f"[DIRECT] Completed provider={provider} downloaded={downloaded}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
