import sys
from pathlib import Path

import netCDF4
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "utils"))

import rewrite_geos_v3_hindcast_leads as rw  # noqa: E402


def _make_v3(path, n=64):
    """GEOS_V3-style hindcast: int64 L offsets, NaN fill, float64 coords, NaN data present."""
    with netCDF4.Dataset(path, "w") as ds:
        for name, size in (("S", 1), ("Y", 4), ("X", 5), ("L", n), ("M", 3)):
            ds.createDimension(name, size)
        s = ds.createVariable("S", "i8", ("S",))
        s.units = "days since 1999-01-01 00:00:00"
        s[:] = 0
        ds.createVariable("L", "i8", ("L",))[:] = np.arange(n)
        m = ds.createVariable("M", "f8", ("M",), fill_value=np.nan)
        m[:] = [1.0, 2.0, 3.0]
        v = ds.createVariable("pr", "f4", ("S", "M", "L", "Y", "X"), fill_value=np.nan)
        v.units = "kg m-2 s-1"
        data = np.arange(3 * n * 4 * 5, dtype="f4").reshape(1, 3, n, 4, 5)
        data[0, 1, 2, 0, 0] = np.nan
        v[:] = data


def _read(path):
    with netCDF4.Dataset(path) as ds:
        ds.set_auto_maskandscale(False)
        return ds["L"][:], ds["L"].dtype, dict((k, ds["L"].getncattr(k)) for k in ds["L"].ncattrs()), ds["pr"][:]


def test_dry_run_changes_nothing(tmp_path):
    f = tmp_path / "pr_GMAO-GEOS_V3_19990101.daily.nc"
    _make_v3(f)
    status, rec = rw.process(str(f), apply=False)
    assert status == "would_rewrite" and rec["new_first"] == 0.5
    assert _read(f)[1] == np.int64


def test_rewrite_sets_float32_half_day_leads_and_keeps_data(tmp_path):
    f = tmp_path / "pr_GMAO-GEOS_V3_19990101.daily.nc"
    _make_v3(f)
    _, _, _, before = _read(f)
    status, _ = rw.process(str(f), apply=True)
    assert status == "rewritten"
    lead, dtype, attrs, after = _read(f)
    assert dtype == np.float32 and lead[0] == 0.5 and lead[-1] == 63.5 and attrs["units"] == "days"
    assert np.array_equal(before, after, equal_nan=True)
    assert not list(tmp_path.glob("*.tmp.nc"))
    assert rw.process(str(f), apply=True)[0] == "already_ok"  # resumable


def test_revert_restores_int_offsets(tmp_path):
    f = tmp_path / "pr_GMAO-GEOS_V3_19990101.daily.nc"
    _make_v3(f, n=90)
    assert rw.process(str(f), apply=True)[0] == "rewritten"
    assert rw.process(str(f), apply=True, reverse=True)[0] == "rewritten"
    lead, dtype, _, _ = _read(f)
    assert dtype == np.int64 and lead[0] == 0 and lead[-1] == 89


def test_unexpected_labels_are_reported_not_touched(tmp_path):
    f = tmp_path / "pr_GMAO-GEOS_V3_19990101.daily.nc"
    _make_v3(f)
    with netCDF4.Dataset(f, "r+") as ds:
        ds["L"][:] = np.arange(1, 65)  # does not start at 0
    status, rec = rw.process(str(f), apply=True)
    assert status == "unexpected"
    assert _read(f)[0][0] == 1
