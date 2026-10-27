"""Direct-ingest L labels must follow the IRIDL / hindcast SubX convention."""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "utils"))

import download_subx_direct as d  # noqa: E402

# (provider, n_leads, first, last) from the IRIDL forecast grids.
IRIDL_GRIDS = [
    ("ECCC", 39, 0.5, 38.5),
    ("CFS", 44, 0.5, 43.5),
    ("FIM", 32, 0.5, 31.5),
    ("GEFS", 34, 1.5, 34.5),
    ("GMAO_V3", 75, 0.5, 74.5),  # real-time GEOS_V3 (variable-length: 63-90 leads)
]


@pytest.mark.parametrize("provider,n,first,last", IRIDL_GRIDS)
def test_lead_grid_matches_iridl(provider, n, first, last):
    lead = d._subx_lead_days(provider, n)
    assert lead.dtype == np.float32
    assert lead[0] == first and lead[-1] == last
    assert len(lead) == n
    assert np.all(np.diff(lead) == 1.0)
