import json
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATE_PATH = REPO_ROOT / "preprocess" / "validate_realtime.py"
_SPEC = importlib.util.spec_from_file_location("validate_realtime", VALIDATE_PATH)
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules["validate_realtime"] = _MODULE
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(_MODULE)
main = _MODULE.main


class ValidateRealtimeTests(unittest.TestCase):
    def write_config(self, root: Path, policy: str, shadow_enabled: bool = False) -> Path:
        config_path = root / "config.yaml"
        lines = [
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
        ]
        if shadow_enabled:
            lines.extend(
                [
                    "ingest:",
                    "  shadow:",
                    "    enabled: true",
                    f"    rt_root: {root / 'shadow_rt'}",
                    "    model_source:",
                    "      GMAO-GEOS_V2p1_5daily: direct_gmao",
                ]
            )

        config_path.write_text(
            "\n".join(lines + [""]),
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

    def test_shadow_parity_manifest_is_written(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = self.write_config(root, "warn_on_missing", shadow_enabled=True)

            primary_pr_dir = root / "rt" / "GMAO-GEOS_V2p1" / "forecast" / "pr"
            shadow_pr_dir = root / "shadow_rt" / "GMAO-GEOS_V2p1" / "forecast" / "pr"
            primary_pr_dir.mkdir(parents=True, exist_ok=True)
            shadow_pr_dir.mkdir(parents=True, exist_ok=True)

            (primary_pr_dir / "pr_GMAO-GEOS_V2p1_20260305.daily.nc").write_text("", encoding="utf-8")
            (shadow_pr_dir / "pr_GMAO-GEOS_V2p1_20260305.daily.nc").write_text("", encoding="utf-8")

            outdir = root / "out"
            exit_code = main(["--config", str(config_path), "--fcstdate", "20260305", "--outdir", str(outdir)])
            self.assertEqual(exit_code, 0)

            parity_manifest_path = outdir / "shadow_parity_manifest.json"
            self.assertTrue(parity_manifest_path.exists())
            manifest = json.loads(parity_manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["summary"]["total"], 2)
            self.assertEqual(manifest["summary"]["presence_ok"], 1)

    def test_schemas_compatible_accepts_dim_aliases_and_var_case(self):
        primary_schema = {
            "dims": {"M": 21, "S": 1, "L": 39, "Y": 181, "X": 360},
            "coords": ["L", "M", "S", "X", "Y"],
            "data_vars": ["pr"],
        }
        shadow_schema = {
            "dims": {"time": 39, "latitude": 181, "longitude": 360},
            "coords": ["latitude", "longitude", "time"],
            "data_vars": ["PR"],
        }
        self.assertTrue(_MODULE._schemas_compatible(primary_schema, shadow_schema, "pr"))

    def test_schemas_compatible_rejects_core_dim_size_mismatch(self):
        primary_schema = {
            "dims": {"L": 39, "Y": 181, "X": 360},
            "coords": ["L", "X", "Y"],
            "data_vars": ["tas"],
        }
        shadow_schema = {
            "dims": {"time": 45, "latitude": 181, "longitude": 360},
            "coords": ["latitude", "longitude", "time"],
            "data_vars": ["tas"],
        }
        self.assertFalse(_MODULE._schemas_compatible(primary_schema, shadow_schema, "tas"))


if __name__ == "__main__":
    unittest.main()