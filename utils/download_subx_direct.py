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
) -> bool:
    var_re = _var_regex(var)
    member_rank = {m.lower(): i for i, m in enumerate(preferred_members)}
    fallback_rank = len(member_rank) + 100

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
            candidates.append((rank, member.name, member))

        if not candidates:
            return False

        # Preferred members first in configured order, then deterministic name order.
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
    provider_url = _cfg_provider_url(cfg, provider)

    print(
        f"[DIRECT] provider={provider} url={provider_url} var={args.var} "
        f"fcst={args.fcst} lookback_days={lookback_days}"
    )

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

    target_dir = Path(args.target_dir)
    downloaded = 0
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
