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
    "eccc": "https://collaboration.cmc.ec.gc.ca/cmc/CMOI/GRIB/GEPS/forecast/subX_fcst/",
}


class RemoteEntry:
    def __init__(self, name, url):
        self.name = name
        self.url = url


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
    return [(end - timedelta(days=delta)).strftime("%Y%m%d") for delta in range(lookback_days + 1)]


def _extract_dates(text: str) -> List[str]:
    return re.findall(r"(20\d{6})", text)


def _var_regex(var):
    return re.compile(rf"(^|[^a-z0-9]){re.escape(var.lower())}([^a-z0-9]|$)")


def _looks_like_data_file(name: str) -> bool:
    lower = name.lower()
    return lower.endswith(".nc") or lower.endswith(".nc4") or lower.endswith(".grb") or lower.endswith(".grib") or lower.endswith(".grib2")


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
        ftp.login("anonymous", email)
        ftp.cwd(base_path)
        names = ftp.nlst()
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


def _list_entries(provider_url: str, ftp_email: str) -> List[RemoteEntry]:
    scheme = urlparse(provider_url).scheme.lower()
    if scheme in {"http", "https"}:
        return _http_list(provider_url, depth=1)
    if scheme == "ftp":
        return _ftp_list(provider_url, ftp_email)
    return []


def _filter_candidates(entries: Sequence[RemoteEntry], var: str, valid_dates: Sequence[str]) -> List[Tuple[str, RemoteEntry]]:
    var_re = _var_regex(var)
    valid_date_set = set(valid_dates)
    candidates: List[Tuple[str, RemoteEntry]] = []
    for entry in entries:
        name = entry.name
        if not _looks_like_data_file(name):
            continue
        lower = name.lower()
        if not var_re.search(lower):
            continue
        date_tokens = _extract_dates(name)
        match_date = next((d for d in date_tokens if d in valid_date_set), None)
        if not match_date:
            continue
        candidates.append((match_date, entry))

    # Newest init first; stable secondary sort by name.
    candidates.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    return candidates


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
            ftp.login("anonymous", ftp_email)
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
        if key == "ECCC-GEPS8":
            return "eccc"
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
    provider_url = _cfg_provider_url(cfg, provider)

    print(
        f"[DIRECT] provider={provider} url={provider_url} var={args.var} "
        f"fcst={args.fcst} lookback_days={lookback_days}"
    )

    entries = _list_entries(provider_url, ftp_email)
    if not entries:
        print(f"[DIRECT][WARN] No entries discovered at provider URL: {provider_url}")
        return 0

    candidates = _filter_candidates(entries, args.var, date_window)
    if not candidates:
        print(
            f"[DIRECT] No matching files found for var={args.var} dates={date_window[-1]}..{date_window[0]} "
            f"from {provider_url}"
        )
        return 0

    target_dir = Path(args.target_dir)
    downloaded = 0
    seen_dates = set()
    for init_date, entry in candidates:
        # Keep the newest candidate per date.
        if init_date in seen_dates:
            continue
        seen_dates.add(init_date)

        out_file = target_dir / f"{args.var}_{args.group}-{args.local_model}_{init_date}.daily.nc"
        if out_file.exists():
            print(f"[DIRECT] Exists, skipping: {out_file}")
            continue

        print(f"[DIRECT] Downloading {entry.url} -> {out_file}")
        try:
            _download_file(entry.url, out_file, ftp_email)
            downloaded += 1
        except Exception as exc:
            print(f"[DIRECT][WARN] Failed download {entry.url}: {exc}", file=sys.stderr)

    print(f"[DIRECT] Completed provider={provider} downloaded={downloaded}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
