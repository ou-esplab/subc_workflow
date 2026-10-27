"""Per-model shape normalization applied to a forecast file before it is appended to Arraylake.

An Arraylake group has fixed ``L`` and ``M`` axes, but some providers vary them per init date.
GEOS_V3's real-time forecasts run 63-90 leads (the end date is fixed, so the length shrinks
by 5 every cycle) and carry 5 members, or 15 on month-end dates.
"""
import numpy as np

# GEOS_V3: keep the first 45 leads (same cap as the products / the retired V2's 45 leads; every
# real-time file has at least 63) and NaN-pad the ensemble axis to the 15-member maximum.
GEOS_V3_N_LEADS = 45
GEOS_V3_N_MEMBERS = 15


def normalize_forecast_dataset(ds, model, datatype):
    """Return ``ds`` with its L/M axes brought to the group's fixed shape (no-op for other models)."""
    if model == "GEOS_V3" and datatype == "forecast":
        ds = ds.isel(L=slice(0, GEOS_V3_N_LEADS))
        ds = ds.reindex(M=np.arange(1, GEOS_V3_N_MEMBERS + 1).astype(ds["M"].dtype))
    return ds
