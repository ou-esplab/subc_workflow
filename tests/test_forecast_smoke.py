import json
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class ForecastSmokeTests(unittest.TestCase):
    def test_forecast_allow_empty_input_writes_manifest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            rt_root = root / "rt"
            hc_root = root / "hc"
            out_weekly = root / "out_weekly"
            out_daily = root / "out_daily"
            rt_root.mkdir(parents=True, exist_ok=True)
            hc_root.mkdir(parents=True, exist_ok=True)

            config_path = root / "config.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        "fcstdate: null",
                        "lon_convention: \"0_360\"",
                        "paths:",
                        f"  rt_root: {rt_root}",
                        f"  hc_root: {hc_root}",
                        f"  out_weekly: {out_weekly}",
                        f"  out_daily: {out_daily}",
                        "models:",
                        "  - { group: EMC, name: GEFSv12_CPC, vars: [pr], levels: [sfc] }",
                        "exceedance:",
                        "  var: pr",
                        "  model_id: EMC-GEFSv12_CPC",
                        "  percentile: 95",
                        "  window_days: 7",
                        "  modelkey: GEFSthres",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            subprocess.run(
                [
                    "python3",
                    str(REPO_ROOT / "products" / "forecast.py"),
                    "--config",
                    str(config_path),
                    "--fcstdate",
                    "20260305",
                    "--save",
                    "--allow-empty-input",
                ],
                cwd=REPO_ROOT,
                check=True,
            )

            manifest_path = out_weekly / "20260305" / "data" / "manifest.json"
            self.assertTrue(manifest_path.exists())
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["smoke_mode"], True)
            self.assertEqual(payload["models"], [])


if __name__ == "__main__":
    unittest.main()