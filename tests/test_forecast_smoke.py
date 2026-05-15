import json
import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
FORECAST_PATH = REPO_ROOT / "products" / "forecast.py"


def _load_forecast_module():
    spec = importlib.util.spec_from_file_location("forecast_module", FORECAST_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


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

    def test_pick_realtime_file_excludes_stale_history(self):
        forecast = _load_forecast_module()
        fcst_ts = forecast.pd.Timestamp("20260515")
        week_start = fcst_ts - forecast.pd.Timedelta(days=7)
        week_end = fcst_ts - forecast.pd.Timedelta(days=1)

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            var_dir = root / "RSMAS-CCSM4" / "forecast" / "pr"
            var_dir.mkdir(parents=True, exist_ok=True)

            # File older than the enforced last-week window.
            (var_dir / "pr_RSMAS-CCSM4_20260501.daily.nc").touch()

            chosen = forecast._pick_realtime_file(
                rt_root=str(root),
                group="RSMAS",
                model_candidates=["CCSM4"],
                var="pr",
                fcst_ts=fcst_ts,
                week_start=week_start,
                week_end=week_end,
            )
            self.assertIsNone(chosen)

    def test_pick_realtime_file_prefers_latest_in_week(self):
        forecast = _load_forecast_module()
        fcst_ts = forecast.pd.Timestamp("20260515")
        week_start = fcst_ts - forecast.pd.Timedelta(days=7)
        week_end = fcst_ts - forecast.pd.Timedelta(days=1)

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            var_dir = root / "EMC-GEFSv12_CPC" / "forecast" / "pr"
            var_dir.mkdir(parents=True, exist_ok=True)

            # Multiple in-window files should select the latest init date.
            (var_dir / "pr_EMC-GEFSv12_CPC_20260510.daily.nc").touch()
            (var_dir / "pr_EMC-GEFSv12_CPC_20260514.daily.nc").touch()

            chosen = forecast._pick_realtime_file(
                rt_root=str(root),
                group="EMC",
                model_candidates=["GEFSv12_CPC"],
                var="pr",
                fcst_ts=fcst_ts,
                week_start=week_start,
                week_end=week_end,
            )

            self.assertIsNotNone(chosen)
            _, chosen_model, chosen_init = chosen
            self.assertEqual(chosen_model, "GEFSv12_CPC")
            self.assertEqual(chosen_init, forecast.pd.Timestamp("20260514"))


if __name__ == "__main__":
    unittest.main()
