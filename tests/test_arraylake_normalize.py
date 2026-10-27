import sys
from pathlib import Path

import numpy as np
import xarray as xr

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "arraylake"))

from normalize_forecast import normalize_forecast_dataset  # noqa: E402


def _ds(n_leads, n_members):
    return xr.Dataset(
        {"pr": (("S", "M", "L", "Y", "X"), np.ones((1, n_members, n_leads, 2, 3), "f4"))},
        coords={"S": [np.datetime64("2026-10-18")], "M": np.arange(1, n_members + 1),
                "L": (np.arange(n_leads) + 0.5).astype("f4"), "Y": [0.0, 1.0], "X": [0.0, 1.0, 2.0]},
    )


def test_geos_v3_forecast_is_capped_and_padded():
    out = normalize_forecast_dataset(_ds(75, 5), "GEOS_V3", "forecast")
    assert out.sizes["L"] == 45 and out.sizes["M"] == 15
    assert out["L"].values[0] == 0.5 and out["L"].values[-1] == 44.5 and out["L"].dtype == np.float32
    assert out["M"].dtype == np.int64 and list(out["M"].values) == list(range(1, 16))
    pr = out["pr"].values
    assert np.isfinite(pr[:, :5]).all() and np.isnan(pr[:, 5:]).all()  # members 6-15 padded, never invented


def test_geos_v3_fifteen_members_keeps_all_data():
    out = normalize_forecast_dataset(_ds(90, 15), "GEOS_V3", "forecast")
    assert out.sizes["L"] == 45 and out.sizes["M"] == 15 and np.isfinite(out["pr"].values).all()


def test_other_models_and_hindcasts_untouched():
    ds = _ds(75, 5)
    assert normalize_forecast_dataset(ds, "GEPS8", "forecast").sizes == ds.sizes
    assert normalize_forecast_dataset(ds, "GEOS_V3", "hindcast").sizes == ds.sizes
