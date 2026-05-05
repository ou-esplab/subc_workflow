import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import runners.cli as cli


class CliWorkflowTests(unittest.TestCase):
    def test_subx_stage_commands_use_absolute_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text("{}\n", encoding="utf-8")
            calls = []

            def fake_run_cmd(command, log_path, extra_env=None):
                calls.append((command, log_path))
                return log_path

            with mock.patch.object(cli, "run_cmd", side_effect=fake_run_cmd):
                with mock.patch("sys.argv", [
                    "cli.py",
                    "--system",
                    "subx",
                    "--config",
                    str(config_path),
                ]):
                    exit_code = cli.main()

            self.assertEqual(exit_code, 0)
            self.assertEqual([Path(call[0][0]).name for call in calls], [
                "update_subx_fcsts.sh",
                "run_addvars_rt.sh",
                "run_preprocess.sh",
                "make_fcsts.sh",
                "pycpt_run.sh",
                "publish_subx_web.sh",
            ])
            self.assertTrue(all(os.path.isabs(call[0][0]) for call in calls))
            self.assertTrue(all(call[0][2] == os.path.abspath(config_path) for call in calls))

    def test_subset_stage_order_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text("{}\n", encoding="utf-8")
            calls = []

            with mock.patch.object(
                cli,
                "run_cmd",
                side_effect=lambda command, log_path, extra_env=None: calls.append((command, log_path)),
            ):
                with mock.patch("sys.argv", [
                    "cli.py",
                    "--system",
                    "subx",
                    "--config",
                    str(config_path),
                    "--init",
                    "20260305",
                    "--stages",
                    "products",
                    "pycpt",
                ]):
                    exit_code = cli.main()

            self.assertEqual(exit_code, 0)
            self.assertEqual([Path(call[0][0]).name for call in calls], ["make_fcsts.sh", "pycpt_run.sh"])


if __name__ == "__main__":
    unittest.main()