import unittest

import numpy as np
import xarray as xr

from forecast import _prepare_domain_view, _resolve_panel_models


class ForecastDomainViewTests(unittest.TestCase):
    def test_prepare_domain_view_handles_descending_latitude(self):
        da = xr.DataArray(
            np.arange(5 * 6).reshape(5, 6),
            dims=("lat", "lon"),
            coords={
                "lat": np.array([75, 60, 45, 30, 15], dtype=float),
                "lon": np.array([220, 230, 240, 250, 260, 270], dtype=float),
            },
        )

        view = _prepare_domain_view(da, lon_bounds=(-140, -90), lat_bounds=(15, 75))

        self.assertGreater(view.sizes["lat"], 0)
        self.assertGreater(view.sizes["lon"], 0)
        self.assertTrue(np.isfinite(view.values).all())


class ForecastPanelModelResolutionTests(unittest.TestCase):
    def test_resolve_panel_models_maps_configured_alias_to_available_model(self):
        available_models = [
            "EMC-GEFSv12_CPC",
            "ESRL-FIMr1p1",
            "RSMAS-CCSM4",
            "GMAO-GEOS_V2p1",
            "ECCC-GEPS8",
            "NCEP-CFSv2",
            "SUBC-MME",
        ]
        configured_models = [
            "EMC-GEFSv12_CPC",
            "ESRL-FIMr1p1",
            "RSMAS-CCSM4",
            "GMAO-GEOS_V2p1_5daily",
            "ECCC-GEPS8",
            "NCEP-CFSv2",
            "SUBC-MME",
        ]

        models = _resolve_panel_models(available_models, configured_models)

        self.assertIn("GMAO-GEOS_V2p1", models)
        self.assertNotIn("GMAO-GEOS_V2p1_5daily", models)

    def test_resolve_panel_models_without_config_uses_available(self):
        available_models = ["A", "B", "SUBC-MME"]
        models = _resolve_panel_models(available_models, panel_models=None)
        self.assertEqual(models, ["A", "B", "SUBC-MME"])


if __name__ == "__main__":
    unittest.main()
