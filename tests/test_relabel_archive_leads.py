import json
import sys
from pathlib import Path

import netCDF4
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "utils"))

import audit_archive_leads as audit  # noqa: E402
import relabel_archive_leads as relabel  # noqa: E402


def _first_lead(path):
    with netCDF4.Dataset(path) as ds:
        return float(ds["L"][0])


def _make(path, lead):
    with netCDF4.Dataset(path, "w") as ds:
        for name, n in (("S", 1), ("M", 2), ("L", len(lead)), ("Y", 4), ("X", 5)):
            ds.createDimension(name, n)
        l = ds.createVariable("L", "f4", ("L",))
        l[:] = lead
        l.units = "days"
        v = ds.createVariable("pr", "f4", ("S", "M", "L", "Y", "X"))
        v[:] = np.arange(1 * 2 * len(lead) * 4 * 5, dtype="f4").reshape(1, 2, len(lead), 4, 5)


def test_audit_relabel_and_revert(tmp_path):
    f = tmp_path / "ECCC-GEPS8" / "forecast" / "pr" / "pr_ECCC-GEPS8_20261008.daily.nc"
    f.parent.mkdir(parents=True)
    _make(f, np.arange(1.0, 40.0))  # raw provider stamps 1.0…39.0

    rec = audit.audit_file(str(f), "ECCC-GEPS8")
    assert rec["status"] == "relabel" and rec["new"][0] == 0.5

    manifest = tmp_path / "manifest.jsonl"
    assert relabel.relabel_one(rec, False, str(manifest)) == "would_relabel"
    assert _first_lead(f) == 1.0

    assert relabel.relabel_one(rec, True, str(manifest)) == "relabeled"
    with netCDF4.Dataset(f) as ds:
        assert ds["L"][0] == 0.5 and ds["L"][-1] == 38.5 and ds["L"].units == "days"
    assert audit.audit_file(str(f), "ECCC-GEPS8") is None

    relabel.revert(type("A", (), {"revert": str(manifest), "apply": True})())
    with netCDF4.Dataset(f) as ds:
        assert ds["L"][0] == 1.0


def test_wrong_length_is_reported_not_touched(tmp_path):
    f = tmp_path / "pr_x.daily.nc"
    _make(f, np.arange(1.0, 30.0))
    rec = audit.audit_file(str(f), "ECCC-GEPS8")
    assert rec["status"] == "length_mismatch"
    assert relabel.relabel_one(rec, True, str(tmp_path / "m.jsonl")) == "skipped"
    assert _first_lead(f) == 1.0


def test_changed_since_audit_is_not_modified(tmp_path):
    f = tmp_path / "pr_x.daily.nc"
    _make(f, np.arange(1.0, 40.0))
    rec = audit.audit_file(str(f), "ECCC-GEPS8")
    _make(f, np.arange(0.5, 39.5))  # file replaced after the audit
    assert relabel.relabel_one(rec, True, str(tmp_path / "m.jsonl")) == "changed_since_audit"


def _make_int_lead(path, lead):
    """GEOS_V3-style hindcast: L stored as int64 day offsets."""
    with netCDF4.Dataset(path, "w") as ds:
        for name, n in (("S", 1), ("M", 2), ("L", len(lead)), ("Y", 4), ("X", 5)):
            ds.createDimension(name, n)
        l = ds.createVariable("L", "i8", ("L",))
        l[:] = lead
        v = ds.createVariable("pr", "f4", ("S", "M", "L", "Y", "X"))
        v[:] = 1.0


def test_hindcast_starts_at_half_day_for_every_model(tmp_path):
    f = tmp_path / "pr_x.daily.nc"
    _make(f, np.arange(1.0, 40.0))  # GEPS8 hindcast stamped 1.0...39.0
    rec = audit.audit_file(str(f), "ECCC-GEPS8", kind="hindcast")
    assert rec["status"] == "relabel" and rec["kind"] == "hindcast"
    assert rec["new"][0] == 0.5 and rec["new"][-1] == 38.5

    g = tmp_path / "pr_gefs.daily.nc"
    _make(g, np.arange(0.5, 35.5))  # GEFS hindcast is 0.5-based with 35 leads (forecast is 1.5/34)
    assert audit.audit_file(str(g), "EMC-GEFSv12_CPC", kind="hindcast") is None
    assert audit.audit_file(str(g), "EMC-GEFSv12_CPC", kind="forecast")["status"] == "length_mismatch"


def test_free_length_model_is_checked_by_arange(tmp_path):
    f = tmp_path / "pr_ccsm4.daily.nc"
    _make(f, np.arange(0.5, 45.5))
    assert audit.audit_file(str(f), "RSMAS-CCSM4", kind="hindcast") is None
    _make(f, np.arange(1.0, 46.0))
    assert audit.audit_file(str(f), "RSMAS-CCSM4", kind="hindcast")["status"] == "relabel"


def test_int_lead_is_dtype_mismatch_and_never_relabeled_in_place(tmp_path):
    f = tmp_path / "pr_geos_v3.daily.nc"
    _make_int_lead(f, np.arange(0, 64))
    rec = audit.audit_file(str(f), "GMAO-GEOS_V3", kind="hindcast")
    assert rec["status"] == "dtype_mismatch" and rec["dtype"] == "int64"
    assert rec["new"][0] == 0.5 and rec["new"][-1] == 63.5
    assert relabel.relabel_one(rec, True, str(tmp_path / "m.jsonl")) == "skipped"
    with netCDF4.Dataset(f) as ds:
        assert ds["L"][0] == 0 and ds["L"].dtype == np.int64
