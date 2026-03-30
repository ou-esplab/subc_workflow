import unittest

import numpy as np
import pandas as pd
import xarray as xr

from utils.subc_pycpt_utils import ensure_lon, fcst_week_dates, latest_thursday, weekly_reduce


class SubcPycptUtilsTests(unittest.TestCase):
    def test_latest_thursday_rounds_back_to_previous_thursday(self):
        self.assertEqual(latest_thursday("20260306"), "20260305")

    def test_fcst_week_dates_returns_seven_day_window(self):
        dates = fcst_week_dates("20260305")
        self.assertEqual(len(dates), 7)
        self.assertEqual(str(dates[0].date()), "2026-02-27")
        self.assertEqual(str(dates[-1].date()), "2026-03-05")

    def test_ensure_lon_converts_negative_to_360(self):
        ds = xr.Dataset(coords={"lon": xr.DataArray(np.array([-10.0, 20.0]), dims=("lon",))})
        converted = ensure_lon(ds, "0_360")
        self.assertEqual(converted.lon.values.tolist(), [20.0, 350.0])

    def test_weekly_reduce_builds_cpc_week_means(self):
        lead = pd.date_range("2026-03-07", periods=28, freq="D")
        ds = xr.Dataset(
            {
                "tas": xr.DataArray(np.ones(28), dims=("lead",), coords={"lead": lead}),
                "pr": xr.DataArray(np.ones(28), dims=("lead",), coords={"lead": lead}),
            }
        )

        reduced = weekly_reduce(ds, "20260305", nweeks=4)
        self.assertEqual(reduced.sizes["week"], 4)
        self.assertTrue((reduced["tas"].values == 1.0).all())
        self.assertTrue((reduced["pr"].values == 7 * 86400.0).all())


if __name__ == "__main__":
    unittest.main()