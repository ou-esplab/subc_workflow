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
        return xr.open_dataset(tmp_file, decode_times=False)

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
        return xr.open_dataset(tmp_file)

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
    combined = combined.expand_dims(S=[np.datetime64(init_ts, "ns")])
    ds_out = combined.to_dataset(name=var)

    out_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_file.with_suffix(".tmp.nc")
    ds_out.to_netcdf(tmp)
    tmp.rename(out_file)
    return True

# ── end ECCC helpers ───────────────────────────────────────────────────────────


# ── RSMAS format conversion ────────────────────────────────────────────────────
# RSMAS files use uppercase dim names (TIME, LAT, LON), cftime NoLeap calendar,
# uppercase variable names (PR, TS, etc.), and a lev_p coordinate for pressure.

def _convert_rsmas_to_subx(path: Path, var: str, init_date: str) -> None:
    """Convert a raw RSMAS file to SubX format (S, M, [P,] L, Y, X) in-place."""
    import numpy as np
    import pandas as pd
    import xarray as xr

    init_ts = pd.Timestamp(init_date)
    ds = xr.open_dataset(path)

    # TIME uses cftime NoLeap; convert to plain timestamps
    time_vals = ds["TIME"].values
    if hasattr(time_vals[0], "year"):
        valid_times = [
            pd.Timestamp(t.year, t.month, t.day, t.hour, t.minute, t.second)
            for t in time_vals
        ]
    else:
        valid_times = list(pd.DatetimeIndex(time_vals))

    lead_days = np.array(
        [(v - init_ts).total_seconds() / 86400.0 for v in valid_times],
        dtype=np.float32,
    )

    # Locate variable (RSMAS uses uppercase: PR → pr)
    var_in_file = next(
        (v for v in ds.data_vars if v.lower() == var.lower() and "bnd" not in v.lower()),
        None,
    )
    if var_in_file is None:
        print(f"[DIRECT][RSMAS][WARN] '{var}' not found in {path.name}; available: {list(ds.data_vars)}")
        return

    # Drop bounds variables and their dimension
    drop = [v for v in ds.data_vars if "bnd" in v.lower() or "bound" in v.lower()]
    ds = ds.drop_vars(drop, errors="ignore")

    lev_name = next((c for c in ["lev_p", "lev", "plev"] if c in ds.coords), None)
    rename_map = {"TIME": "L", "LAT": "Y", "LON": "X"}
    if lev_name:
        rename_map[lev_name] = "P"

    da = ds[var_in_file].rename(rename_map).assign_coords(L=lead_days)
    da["L"].attrs["units"] = "days"
    if "P" in da.coords:
        da = da.assign_coords(P=da["P"].values.astype(int))

    da = da.expand_dims(S=[np.datetime64(init_ts, "ns")], M=[1])
    da.name = var
    ds_out = da.to_dataset()
    ds.close()

    tmp = path.with_suffix(".tmp.nc")
    ds_out.to_netcdf(tmp)
    tmp.rename(path)

# ── end RSMAS helpers ──────────────────────────────────────────────────────────


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

    entries = _list_entries(provider_url, ftp_email, provider, date_window)
    if not entries:
        print(f"[DIRECT][WARN] No entries discovered at provider URL: {provider_url}")
        return 0

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
                if provider == "rsmas":
                    _convert_rsmas_to_subx(out_file, args.var, init_date)
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
