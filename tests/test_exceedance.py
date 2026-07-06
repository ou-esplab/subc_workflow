#!/usr/bin/env python3
"""Unit tests for compute_exceedance and nearest_mmdd_threshold."""

import os
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

from utils.exceedance_utils import compute_exceedance, nearest_mmdd_threshold


class NearestMmddThresholdTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.thr_dir = self._tmpdir.name
        self.var = "pr"
        self.group = "EMC"
        self.model = "GEFSv12_CPC"
        self.pct = 95

    def tearDown(self):
        self._tmpdir.cleanup()

    def _touch(self, mmdd: str) -> str:
        fname = f"{self.var}_{self.group}-{self.model}_{mmdd}.{self.pct}p.nc"
        path = os.path.join(self.thr_dir, fname)
        Path(path).touch()
        return path

    def test_exact_match(self):
        path = self._touch("0305")
        result = nearest_mmdd_threshold(self.thr_dir, self.var, self.group, self.model, "0305", self.pct)
        self.assertEqual(result, path)

    def test_nearest_within_offset(self):
        path = self._touch("0308")  # 3 days from 0305
        result = nearest_mmdd_threshold(
            self.thr_dir, self.var, self.group, self.model, "0305", self.pct, max_offset_days=7
        )
        self.assertEqual(result, path)

    def test_beyond_max_offset_returns_none(self):
        self._touch("0320")  # 15 days from 0305
        result = nearest_mmdd_threshold(
            self.thr_dir, self.var, self.group, self.model, "0305", self.pct, max_offset_days=7
        )
        self.assertIsNone(result)

    def test_no_candidates_returns_none(self):
        result = nearest_mmdd_threshold(self.thr_dir, self.var, self.group, self.model, "0305", self.pct)
        self.assertIsNone(result)

    def test_prefers_exact_over_nearby(self):
        self._touch("0303")  # 2 days off
        exact = self._touch("0305")
        result = nearest_mmdd_threshold(self.thr_dir, self.var, self.group, self.model, "0305", self.pct)
        self.assertEqual(result, exact)

    def test_year_boundary_wrap(self):
        """Circular day-of-year distance wraps correctly across the Dec/Jan boundary."""
        # Jan 3 is ~5 circular days from Dec 30 (using 1900 non-leap arithmetic)
        path = self._touch("0103")
        result = nearest_mmdd_threshold(
            self.thr_dir, self.var, self.group, self.model, "1230", self.pct, max_offset_days=7
        )
        self.assertEqual(result, path)


class ComputeExceedanceTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.lat = np.array([10.0, 20.0])
        self.lon = np.array([100.0, 110.0])
        self.n_lead = 14
        self.n_ens = 4
        self.init_date = pd.Timestamp("2026-03-05")

    def tearDown(self):
        self._tmpdir.cleanup()

    def _make_thr(self, threshold_val: float, n_lead: int = None) -> str:
        n = n_lead if n_lead is not None else self.n_lead
        path = os.path.join(self._tmpdir.name, "thr.nc")
        ds = xr.Dataset(
            {"pr": (("lead", "lat", "lon"), np.full((n, len(self.lat), len(self.lon)), threshold_val))},
            coords={"lead": np.arange(n), "lat": self.lat, "lon": self.lon},
        )
        ds.to_netcdf(path)
        return path

    def _make_field(self, values) -> xr.DataArray:
        lead = pd.date_range(self.init_date, periods=self.n_lead, freq="D")
        if np.isscalar(values):
            data = np.full((self.n_lead, self.n_ens, len(self.lat), len(self.lon)), float(values))
        else:
            data = np.asarray(values, dtype=float)
        return xr.DataArray(
            data,
            dims=("lead", "M", "lat", "lon"),
            coords={"lead": lead, "M": np.arange(self.n_ens), "lat": self.lat, "lon": self.lon},
            name="pr",
        )

    def test_all_exceed_gives_100_percent(self):
        thr_path = self._make_thr(1.0)
        probs = compute_exceedance(self._make_field(10.0), thr_path, self.init_date, window=7, var="pr")
        self.assertTrue(np.allclose(probs.values, 100.0))

    def test_none_exceed_gives_0_percent(self):
        thr_path = self._make_thr(100.0)
        probs = compute_exceedance(self._make_field(1.0), thr_path, self.init_date, window=7, var="pr")
        self.assertTrue(np.allclose(probs.values, 0.0))

    def test_partial_exceedance_probability(self):
        """2 of 4 members exceed threshold on every lead → 50%."""
        thr_path = self._make_thr(5.0)
        data = np.zeros((self.n_lead, self.n_ens, len(self.lat), len(self.lon)))
        data[:, :2, :, :] = 10.0  # members 0 and 1 exceed
        probs = compute_exceedance(self._make_field(data), thr_path, self.init_date, window=7, var="pr")
        self.assertTrue(np.allclose(probs.values, 50.0))

    def test_return_counts_instead_of_probability(self):
        thr_path = self._make_thr(1.0)
        counts = compute_exceedance(
            self._make_field(10.0), thr_path, self.init_date, window=7, var="pr", return_counts=True
        )
        self.assertTrue(np.all(counts.values == self.n_ens))

    def test_output_has_time_window_dim(self):
        thr_path = self._make_thr(1.0)
        probs = compute_exceedance(self._make_field(10.0), thr_path, self.init_date, window=7, var="pr")
        self.assertIn("time_window", probs.dims)

    def test_window_reduces_output_length(self):
        """Output time_window count = n_lead - (window - 1)."""
        window = 7
        thr_path = self._make_thr(1.0)
        probs = compute_exceedance(self._make_field(10.0), thr_path, self.init_date, window=window, var="pr")
        self.assertEqual(probs.sizes["time_window"], self.n_lead - (window - 1))

    def test_window_1_preserves_all_leads(self):
        thr_path = self._make_thr(1.0)
        probs = compute_exceedance(self._make_field(10.0), thr_path, self.init_date, window=1, var="pr")
        self.assertEqual(probs.sizes["time_window"], self.n_lead)

    def test_lead_shorter_than_window_returns_nan(self):
        """When common lead length < window the function returns early with all-NaN output."""
        n_short = 3
        thr_path = self._make_thr(1.0, n_lead=n_short)
        lead = pd.date_range(self.init_date, periods=n_short, freq="D")
        field = xr.DataArray(
            np.ones((n_short, self.n_ens, len(self.lat), len(self.lon))),
            dims=("lead", "M", "lat", "lon"),
            coords={"lead": lead, "M": np.arange(self.n_ens), "lat": self.lat, "lon": self.lon},
        )
        result = compute_exceedance(field, thr_path, self.init_date, window=7, var="pr")
        self.assertTrue(np.all(np.isnan(result.values)))

    def test_missing_variable_raises_key_error(self):
        thr_path = self._make_thr(1.0)
        with self.assertRaises(KeyError):
            compute_exceedance(self._make_field(10.0), thr_path, self.init_date, window=7, var="tas")

    def test_any_day_in_window_triggers_exceedance(self):
        """Only day 0 exceeds: first window (days 0–6) is 100%, final window (days 7–13) is 0%."""
        thr_path = self._make_thr(5.0)
        data = np.zeros((self.n_lead, self.n_ens, len(self.lat), len(self.lon)))
        data[0, :, :, :] = 10.0  # only first day exceeds
        probs = compute_exceedance(self._make_field(data), thr_path, self.init_date, window=7, var="pr")
        self.assertTrue(np.allclose(probs.isel(time_window=0).values, 100.0))
        self.assertTrue(np.allclose(probs.isel(time_window=-1).values, 0.0))

    def test_below_direction_all_under_threshold_gives_100_percent(self):
        thr_path = self._make_thr(5.0)
        probs = compute_exceedance(
            self._make_field(1.0), thr_path, self.init_date, window=7, var="pr", direction="below"
        )
        self.assertTrue(np.allclose(probs.values, 100.0))

    def test_below_direction_none_under_threshold_gives_0_percent(self):
        thr_path = self._make_thr(5.0)
        probs = compute_exceedance(
            self._make_field(10.0), thr_path, self.init_date, window=7, var="pr", direction="below"
        )
        self.assertTrue(np.allclose(probs.values, 0.0))

    def test_all_aggregate_requires_every_day_in_window(self):
        """With aggregate='all', a single day back above threshold breaks an otherwise-dry window."""
        thr_path = self._make_thr(5.0)
        data = np.full((self.n_lead, self.n_ens, len(self.lat), len(self.lon)), 1.0)  # every day dry
        data[3, :, :, :] = 10.0  # day 3 is not dry, breaks any window containing it
        probs = compute_exceedance(
            self._make_field(data), thr_path, self.init_date, window=7, var="pr",
            direction="below", aggregate="all",
        )
        # First window (days 0-6) contains day 3 -> not all dry -> 0%.
        self.assertTrue(np.allclose(probs.isel(time_window=0).values, 0.0))
        # Last window (days 7-13) excludes day 3 -> all dry -> 100%.
        self.assertTrue(np.allclose(probs.isel(time_window=-1).values, 100.0))

    def test_any_aggregate_vs_all_aggregate_differ(self):
        """A single dry day should trigger aggregate='any' but not aggregate='all'."""
        thr_path = self._make_thr(5.0)
        data = np.full((self.n_lead, self.n_ens, len(self.lat), len(self.lon)), 10.0)  # not dry
        data[0, :, :, :] = 1.0  # only day 0 is dry
        any_probs = compute_exceedance(
            self._make_field(data), thr_path, self.init_date, window=7, var="pr",
            direction="below", aggregate="any",
        )
        all_probs = compute_exceedance(
            self._make_field(data), thr_path, self.init_date, window=7, var="pr",
            direction="below", aggregate="all",
        )
        self.assertTrue(np.allclose(any_probs.isel(time_window=0).values, 100.0))
        self.assertTrue(np.allclose(all_probs.isel(time_window=0).values, 0.0))

    def test_invalid_direction_raises_value_error(self):
        thr_path = self._make_thr(1.0)
        with self.assertRaises(ValueError):
            compute_exceedance(self._make_field(10.0), thr_path, self.init_date, window=7, var="pr", direction="sideways")

    def test_invalid_aggregate_raises_value_error(self):
        thr_path = self._make_thr(1.0)
        with self.assertRaises(ValueError):
            compute_exceedance(self._make_field(10.0), thr_path, self.init_date, window=7, var="pr", aggregate="most")

    def test_regional_subset_reduces_domain(self):
        """lon_slice and lat_slice restrict the output spatial domain."""
        thr_path = self._make_thr(1.0)
        probs = compute_exceedance(
            self._make_field(10.0),
            thr_path,
            self.init_date,
            window=7,
            var="pr",
            lon_slice=(100.0, 105.0),  # selects only lon=100.0
            lat_slice=(10.0, 15.0),    # selects only lat=10.0
        )
        self.assertEqual(probs.sizes["lon"], 1)
        self.assertEqual(probs.sizes["lat"], 1)


if __name__ == "__main__":
    unittest.main()
