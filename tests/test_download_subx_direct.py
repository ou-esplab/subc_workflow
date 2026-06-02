import io
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from utils import download_subx_direct as dsd


class _FakeResp:
    def __init__(self, text):
        self._text = text

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._text.encode("utf-8")


class DownloadSubxDirectUnitTests(unittest.TestCase):
    def test_cfg_eccc_members_range(self):
        cfg = {"ingest": {"direct": {"eccc": {"member": "m00-m20"}}}}
        members = dsd._cfg_eccc_members(cfg)
        self.assertEqual(members[0], "m00")
        self.assertEqual(members[-1], "m20")
        self.assertEqual(len(members), 21)

    def test_http_list_eccc_walks_date_subdirectories(self):
        base_url = "https://example.test/subX_fcst/"
        root_html = (
            '<a href="20260528/">20260528/</a>'
            '<a href="20260601/">20260601/</a>'
            '<a href="README/">README/</a>'
        )
        date_html = '<a href="subx_20260528_bundle.tar">subx_20260528_bundle.tar</a>'

        responses = {
            base_url: root_html,
            base_url + "20260528/": date_html,
        }

        def fake_urlopen(url, timeout=30):  # pylint: disable=unused-argument
            if url not in responses:
                raise RuntimeError("unexpected URL: %s" % url)
            return _FakeResp(responses[url])

        with mock.patch.object(dsd, "urlopen", side_effect=fake_urlopen):
            entries = dsd._http_list_eccc(base_url, valid_dates=["20260528"])

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].name, "subx_20260528_bundle.tar")
        self.assertEqual(entries[0].url, base_url + "20260528/subx_20260528_bundle.tar")

    def test_http_list_gmao_v3_walks_date_subdirectories(self):
        base_url = "https://example.test/SubC/"
        root_html = (
            '<a href="20260528/">20260528/</a>'
            '<a href="20260531/">20260531/</a>'
            '<a href="README/">README/</a>'
        )
        date_html = '<a href="pr_sfc_GMAOGEOS_20260528_ens01.nc4">pr file</a>'

        responses = {
            base_url: root_html,
            base_url + "20260528/": date_html,
        }

        def fake_urlopen(url, timeout=30):  # pylint: disable=unused-argument
            if url not in responses:
                raise RuntimeError("unexpected URL: %s" % url)
            return _FakeResp(responses[url])

        with mock.patch.object(dsd, "urlopen", side_effect=fake_urlopen):
            entries = dsd._http_list_gmao_v3(base_url, valid_dates=["20260528"])

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].name, "pr_sfc_GMAOGEOS_20260528_ens01.nc4")
        self.assertEqual(entries[0].url, base_url + "20260528/pr_sfc_GMAOGEOS_20260528_ens01.nc4")

    def test_extract_from_tar_prefers_first_available_member(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            tar_path = root / "bundle_20260528.tar"
            out_file = root / "pr_ECCC-GEPS8_20260528.daily.nc"

            with tarfile.open(str(tar_path), mode="w") as tar:
                for member_name in [
                    "pr_GEPS8_20260528_m05.nc",
                    "pr_GEPS8_20260528_m02.nc",
                ]:
                    payload = ("content-" + member_name).encode("utf-8")
                    info = tarfile.TarInfo(name=member_name)
                    info.size = len(payload)
                    tar.addfile(info, io.BytesIO(payload))

            ok = dsd._extract_from_tar(
                tar_path=tar_path,
                destination=out_file,
                var="pr",
                preferred_members=["m00", "m01", "m02", "m03"],
            )

            self.assertTrue(ok)
            self.assertTrue(out_file.exists())
            content = out_file.read_bytes().decode("utf-8")
            self.assertIn("m02", content)

    def test_provider_from_source_maps_rsmas_direct_keyword(self):
        provider = dsd._provider_from_source("direct_rsmas", "RSMAS", "CCSM4")
        self.assertEqual(provider, "rsmas")

    def test_provider_from_source_maps_rsmas_from_generic_direct(self):
        provider = dsd._provider_from_source("direct", "RSMAS", "CCSM4")
        self.assertEqual(provider, "rsmas")

    def test_provider_from_source_maps_gmao_v3_direct_keyword(self):
        provider = dsd._provider_from_source("direct_gmao_v3", "GMAO", "GEOS_V3")
        self.assertEqual(provider, "gmao_v3")

    def test_provider_from_source_maps_gmao_v3_from_generic_direct(self):
        provider = dsd._provider_from_source("direct", "GMAO", "GEOS_V3")
        self.assertEqual(provider, "gmao_v3")

    def test_lookback_dates_uses_exact_lookback_days(self):
        dates = dsd._lookback_dates("20260528", 7)
        self.assertEqual(len(dates), 7)
        self.assertEqual(dates[0], "20260528")
        self.assertEqual(dates[-1], "20260522")

    def test_lookback_dates_has_minimum_one_day(self):
        dates = dsd._lookback_dates("20260528", 0)
        self.assertEqual(dates, ["20260528"])

    def test_ftp_list_retries_login_with_fallback_credentials(self):
        class _FakeFTP:
            def __init__(self, host, timeout=30):  # pylint: disable=unused-argument
                self.logins = []

            def login(self, user, passwd):
                self.logins.append((user, passwd))
                if passwd == "bad@example.com":
                    raise RuntimeError("530 Login incorrect.")

            def cwd(self, path):  # pylint: disable=unused-argument
                return None

            def nlst(self):
                return ["pr_test_20260528.nc"]

            def quit(self):
                return None

        with mock.patch.object(dsd, "FTP", _FakeFTP):
            entries = dsd._ftp_list("ftp://example.test/subx/", "bad@example.com")

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].name, "pr_test_20260528.nc")


if __name__ == "__main__":
    unittest.main()
