import json
import tempfile
import unittest
from pathlib import Path

from validate_realtime import main


class ValidateRealtimeTests(unittest.TestCase):
    def write_config(self, root: Path, policy: str) -> Path:
        config_path = root / "config.yaml"
        config_path.write_text(
            "\n".join(
                [
                    "paths:",
                    f"  rt_root: {root / 'rt'}",
                    "models:",
                    "  - group: GMAO",
                    "    name: GEOS_V2p1_5daily",
                    "    vars: [pr, tas]",
                    "    levels: [sfc, 2m]",
                    "model_name_map:",
                    "  GMAO-GEOS_V2p1_5daily: GEOS_V2p1",
                    "validation:",
                    f"  policy: {policy}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return config_path

    def test_validation_passes_when_all_expected_files_exist(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = self.write_config(root, "fail_on_missing")
            rt_dir = root / "rt" / "GMAO-GEOS_V2p1" / "forecast"
            (rt_dir / "pr").mkdir(parents=True, exist_ok=True)
            (rt_dir / "tas").mkdir(parents=True, exist_ok=True)
            (rt_dir / "pr" / "pr_GMAO-GEOS_V2p1_20260305_member01.daily.nc").write_text("", encoding="utf-8")
            (rt_dir / "tas" / "tas_GMAO-GEOS_V2p1_20260305_member01.daily.nc").write_text("", encoding="utf-8")

            outdir = root / "out"
            exit_code = main(["--config", str(config_path), "--fcstdate", "20260305", "--outdir", str(outdir)])

            self.assertEqual(exit_code, 0)
            manifest = json.loads((outdir / "validation_manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(all(record["ok"] for record in manifest["records"]))

    def test_validation_fails_when_required_files_are_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = self.write_config(root, "fail_on_missing")

            exit_code = main(["--config", str(config_path), "--fcstdate", "20260305"])
            self.assertEqual(exit_code, 1)

    def test_validation_warn_policy_returns_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = self.write_config(root, "warn")

            exit_code = main(["--config", str(config_path), "--fcstdate", "20260305"])
            self.assertEqual(exit_code, 0)


if __name__ == "__main__":
    unittest.main()