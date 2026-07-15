import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class DownloadScriptTests(unittest.TestCase):
    def test_stub_mode_writes_request_marker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config_path.write_text(f"paths:\n  rt_root: {root / 'rt'}\n", encoding="utf-8")

            env = os.environ.copy()
            env["SUBX_CONFIG"] = str(config_path)
            env["SUBX_DOWNLOAD_STUB"] = "1"

            subprocess.run(
                [
                    str(REPO_ROOT / "ingest" / "download_subx_rtfcst.sh"),
                    "GMAO",
                    "GEOS_V2p1_5daily",
                    "pr",
                    "forecast",
                    "20260305",
                    "GEOS_V2p1",
                ],
                check=True,
                cwd=REPO_ROOT,
                env=env,
            )

            marker = root / "rt" / "GMAO-GEOS_V2p1" / "forecast" / "pr" / "pr_GMAO-GEOS_V2p1_20260305.download-request.json"
            self.assertTrue(marker.exists())
            payload = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(payload["mode"], "stub")
            self.assertEqual(payload["source"], "iridl")

    def test_update_script_passes_model_source_to_downloader(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        "paths:",
                        f"  rt_root: {root / 'rt'}",
                        "concurrency:",
                        "  downloads: 1",
                        "ingest:",
                        "  source_default: iridl",
                        "  primary_enabled: true",
                        "  model_source:",
                        "    ESRL-FIMr1p1: direct_esrl",
                        "models:",
                        "  - group: ESRL",
                        "    name: FIMr1p1",
                        "    vars: [pr]",
                        "    levels: [sfc]",
                        "  - group: EMC",
                        "    name: GEFSv12_CPC",
                        "    vars: [pr]",
                        "    levels: [sfc]",
                    ]
                ),
                encoding="utf-8",
            )

            env = os.environ.copy()
            env["SUBX_DOWNLOAD_STUB"] = "1"

            subprocess.run(
                [
                    str(REPO_ROOT / "ingest" / "update_subx_fcsts.sh"),
                    "20260305",
                    str(config_path),
                ],
                check=True,
                cwd=REPO_ROOT,
                env=env,
            )

            esrl_marker = (
                root
                / "rt"
                / "ESRL-FIMr1p1"
                / "forecast"
                / "pr"
                / "pr_ESRL-FIMr1p1_20260305.download-request.json"
            )
            emc_marker = (
                root
                / "rt"
                / "EMC-GEFSv12_CPC"
                / "forecast"
                / "pr"
                / "pr_EMC-GEFSv12_CPC_20260305.download-request.json"
            )
            self.assertTrue(esrl_marker.exists())
            self.assertTrue(emc_marker.exists())

            esrl_payload = json.loads(esrl_marker.read_text(encoding="utf-8"))
            emc_payload = json.loads(emc_marker.read_text(encoding="utf-8"))
            self.assertEqual(esrl_payload["source"], "direct_esrl")
            self.assertEqual(emc_payload["source"], "iridl")

    def test_shadow_mode_downloads_direct_to_shadow_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        "paths:",
                        f"  rt_root: {root / 'rt'}",
                        "concurrency:",
                        "  downloads: 1",
                        "ingest:",
                        "  source_default: iridl",
                        "  primary_enabled: true",
                        "  model_source: {}",
                        "  shadow:",
                        "    enabled: true",
                        f"    rt_root: {root / 'shadow_rt'}",
                        "    model_source:",
                        "      ESRL-FIMr1p1: direct_esrl",
                        "models:",
                        "  - group: ESRL",
                        "    name: FIMr1p1",
                        "    vars: [pr]",
                        "    levels: [sfc]",
                    ]
                ),
                encoding="utf-8",
            )

            env = os.environ.copy()
            env["SUBX_DOWNLOAD_STUB"] = "1"

            subprocess.run(
                [
                    str(REPO_ROOT / "ingest" / "update_subx_fcsts.sh"),
                    "20260305",
                    str(config_path),
                ],
                check=True,
                cwd=REPO_ROOT,
                env=env,
            )

            primary_marker = (
                root
                / "rt"
                / "ESRL-FIMr1p1"
                / "forecast"
                / "pr"
                / "pr_ESRL-FIMr1p1_20260305.download-request.json"
            )
            shadow_marker = (
                root
                / "shadow_rt"
                / "ESRL-FIMr1p1"
                / "forecast"
                / "pr"
                / "pr_ESRL-FIMr1p1_20260305.download-request.json"
            )

            self.assertTrue(primary_marker.exists())
            self.assertTrue(shadow_marker.exists())

            primary_payload = json.loads(primary_marker.read_text(encoding="utf-8"))
            shadow_payload = json.loads(shadow_marker.read_text(encoding="utf-8"))
            self.assertEqual(primary_payload["source"], "iridl")
            self.assertEqual(shadow_payload["source"], "direct_esrl")

    def test_shadow_only_mode_skips_primary_downloads(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        "paths:",
                        f"  rt_root: {root / 'rt'}",
                        "concurrency:",
                        "  downloads: 1",
                        "ingest:",
                        "  source_default: iridl",
                        "  primary_enabled: false",
                        "  model_source: {}",
                        "  shadow:",
                        "    enabled: true",
                        f"    rt_root: {root / 'shadow_rt'}",
                        "    model_source:",
                        "      ESRL-FIMr1p1: direct_esrl",
                        "models:",
                        "  - group: ESRL",
                        "    name: FIMr1p1",
                        "    vars: [pr]",
                        "    levels: [sfc]",
                    ]
                ),
                encoding="utf-8",
            )

            env = os.environ.copy()
            env["SUBX_DOWNLOAD_STUB"] = "1"

            subprocess.run(
                [
                    str(REPO_ROOT / "ingest" / "update_subx_fcsts.sh"),
                    "20260305",
                    str(config_path),
                ],
                check=True,
                cwd=REPO_ROOT,
                env=env,
            )

            primary_marker = (
                root
                / "rt"
                / "ESRL-FIMr1p1"
                / "forecast"
                / "pr"
                / "pr_ESRL-FIMr1p1_20260305.download-request.json"
            )
            shadow_marker = (
                root
                / "shadow_rt"
                / "ESRL-FIMr1p1"
                / "forecast"
                / "pr"
                / "pr_ESRL-FIMr1p1_20260305.download-request.json"
            )

            self.assertFalse(primary_marker.exists())
            self.assertTrue(shadow_marker.exists())


    def test_geos_v3_stub_mode_writes_request_marker(self):
        """When no model_source override is configured, GEOS_V3 falls back to
        source_default (iridl) like any other model -- this test exercises
        that fallback in isolation. Production config.yaml now overrides
        GMAO-GEOS_V3 to direct_gmao_v3 (see
        test_update_script_routes_geos_v3_to_direct_gmao_v3 below)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config_path.write_text(f"paths:\n  rt_root: {root / 'rt'}\n", encoding="utf-8")

            env = os.environ.copy()
            env["SUBX_CONFIG"] = str(config_path)
            env["SUBX_DOWNLOAD_STUB"] = "1"

            subprocess.run(
                [
                    str(REPO_ROOT / "ingest" / "download_subx_rtfcst.sh"),
                    "GMAO",
                    "GEOS_V3",
                    "pr",
                    "forecast",
                    "20260305",
                ],
                check=True,
                cwd=REPO_ROOT,
                env=env,
            )

            marker = (
                root
                / "rt"
                / "GMAO-GEOS_V3"
                / "forecast"
                / "pr"
                / "pr_GMAO-GEOS_V3_20260305.download-request.json"
            )
            self.assertTrue(marker.exists(), f"Expected stub marker at {marker}")
            payload = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(payload["mode"], "stub")
            self.assertEqual(payload["source"], "iridl")

    def test_update_script_includes_geos_v3(self):
        """update_subx_fcsts.sh picks up GEOS_V3 from the models list. This
        inline config sets model_source: {} explicitly (no override), so it
        remains a valid test of the config-wiring-picks-up-whatever-models-
        list-regardless-of-source path; production config.yaml overrides
        GMAO-GEOS_V3 to direct_gmao_v3 (see the sibling test below)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        "paths:",
                        f"  rt_root: {root / 'rt'}",
                        "concurrency:",
                        "  downloads: 1",
                        "ingest:",
                        "  source_default: iridl",
                        "  primary_enabled: true",
                        "  model_source: {}",
                        "models:",
                        "  - group: GMAO",
                        "    name: GEOS_V3",
                        "    vars: [pr]",
                        "    levels: [sfc]",
                    ]
                ),
                encoding="utf-8",
            )

            env = os.environ.copy()
            env["SUBX_DOWNLOAD_STUB"] = "1"

            subprocess.run(
                [
                    str(REPO_ROOT / "ingest" / "update_subx_fcsts.sh"),
                    "20260305",
                    str(config_path),
                ],
                check=True,
                cwd=REPO_ROOT,
                env=env,
            )

            marker = (
                root
                / "rt"
                / "GMAO-GEOS_V3"
                / "forecast"
                / "pr"
                / "pr_GMAO-GEOS_V3_20260305.download-request.json"
            )
            self.assertTrue(marker.exists(), f"Expected stub marker at {marker}")
            payload = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(payload["source"], "iridl")

    def test_update_script_routes_geos_v3_to_direct_gmao_v3(self):
        """With model_source overridden (as production config.yaml now is),
        GEOS_V3 routes to direct_gmao_v3 -- proves the shell-to-Python
        routing end-to-end, complementing the Python-level unit tests in
        test_download_subx_direct.py."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        "paths:",
                        f"  rt_root: {root / 'rt'}",
                        "concurrency:",
                        "  downloads: 1",
                        "ingest:",
                        "  source_default: iridl",
                        "  primary_enabled: true",
                        "  model_source:",
                        "    GMAO-GEOS_V3: direct_gmao_v3",
                        "models:",
                        "  - group: GMAO",
                        "    name: GEOS_V3",
                        "    vars: [pr]",
                        "    levels: [sfc]",
                    ]
                ),
                encoding="utf-8",
            )

            env = os.environ.copy()
            env["SUBX_DOWNLOAD_STUB"] = "1"

            subprocess.run(
                [
                    str(REPO_ROOT / "ingest" / "update_subx_fcsts.sh"),
                    "20260305",
                    str(config_path),
                ],
                check=True,
                cwd=REPO_ROOT,
                env=env,
            )

            marker = (
                root
                / "rt"
                / "GMAO-GEOS_V3"
                / "forecast"
                / "pr"
                / "pr_GMAO-GEOS_V3_20260305.download-request.json"
            )
            self.assertTrue(marker.exists(), f"Expected stub marker at {marker}")
            payload = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(payload["source"], "direct_gmao_v3")


if __name__ == "__main__":
    unittest.main()