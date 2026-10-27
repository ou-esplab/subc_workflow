import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gdex"))

import resend_changed_to_gdex as rs  # noqa: E402


def test_collect_jobs_filters_and_dedupes(tmp_path):
    root = tmp_path / "root"
    keep = root / "ECCC-GEPS8" / "forecast" / "pr" / "pr_ECCC-GEPS8_20260608.daily.nc"
    other_var = root / "ECCC-GEPS8" / "forecast" / "vas" / "vas_ECCC-GEPS8_20260608.daily.nc"
    other_model = root / "NCEP-CFSv2" / "hindcast" / "pr" / "pr_NCEP-CFSv2_20010101.daily.nc"
    tmpf = root / "ECCC-GEPS8" / "forecast" / "pr" / "pr_ECCC-GEPS8_20260609.tmp.nc"
    gone = root / "ECCC-GEPS8" / "forecast" / "pr" / "pr_ECCC-GEPS8_20260610.daily.nc"
    for f in (keep, other_var, other_model, tmpf):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x")
    m1, m2 = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    rows = [keep, keep, other_var, other_model, tmpf, gone, tmp_path / "elsewhere.nc"]
    m1.write_text("\n".join(json.dumps({"path": str(p)}) for p in rows[:4]) + "\n")
    m2.write_text("\n".join(json.dumps({"path": str(p)}) for p in rows[4:]) + "\n\n")

    jobs, skipped = rs.collect_jobs([m1, m2], str(root), {"ECCC-GEPS8": {"pr"}})
    assert jobs == {"ECCC-GEPS8": [("ECCC-GEPS8/forecast/pr/pr_ECCC-GEPS8_20260608.daily.nc",) * 2]}
    assert skipped == {"outside_root": 1, "not_mirrored": 2, "missing_locally": 1, "tmp": 1}
