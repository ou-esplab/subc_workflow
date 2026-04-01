import os
import tempfile
import unittest

import numpy as np
import xarray as xr

from forecast import _write_backward_compat_pr_anom_files


class ForecastBackwardCompatOutputTests(unittest.TestCase):
    def test_write_backward_compat_pr_anom_files_matches_legacy_shape(self):
        lat = np.linspace(-20, 20, 4)
        lon = np.linspace(0, 30, 5)
        week = np.array([1, 2, 3, 4], dtype=np.int32)
        model = np.array([
            "NCEP-CFSv2",
            "EMC-GEFSv12_CPC",
            "ESRL-FIMr1p1",
            "SUBC-MME",
        ], dtype=object)
        members = np.array([1, 2, 3], dtype=np.int32)

        # week, model, member, lat, lon
        values = np.random.RandomState(1).randn(
            week.size,
            model.size,
            members.size,
            lat.size,
            lon.size,
        )

        ds = xr.Dataset(
            {
                "pr_members": (("week", "model", "M", "lat", "lon"), values),
            },
            coords={
                "week": week,
                "model": model,
                "M": members,
                "lat": lat,
                "lon": lon,
            },
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            _write_backward_compat_pr_anom_files(ds, tmpdir, "20260326")

            for stat in ["emin", "emean", "emax"]:
                path = os.path.join(tmpdir, f"fcst_20260326.anom.pr_sfc.{stat}.nc")
                self.assertTrue(os.path.exists(path), msg=f"Missing output file: {path}")

                out = xr.open_dataset(path)
                try:
                    # Legacy compatibility: dimensions are time/lat/lon (no model dimension).
                    self.assertIn("time", out.dims)
                    self.assertIn("lat", out.dims)
                    self.assertIn("lon", out.dims)
                    self.assertNotIn("model", out.dims)

                    self.assertEqual(int(out.sizes["time"]), 4)
                    self.assertEqual(int(out.sizes["lat"]), lat.size)
                    self.assertEqual(int(out.sizes["lon"]), lon.size)
                    self.assertTrue(np.issubdtype(out["time"].dtype, np.datetime64))

                    # Legacy compatibility: one variable per model plus MME.
                    self.assertIn("CFSv2", out.data_vars)
                    self.assertIn("GEFSv12_CPC", out.data_vars)
                    self.assertIn("FIMr1p1", out.data_vars)
                    self.assertIn("MME", out.data_vars)

                    for v in ["CFSv2", "GEFSv12_CPC", "FIMr1p1", "MME"]:
                        self.assertEqual(out[v].dims, ("time", "lat", "lon"))
                finally:
                    out.close()


if __name__ == "__main__":
    unittest.main()
