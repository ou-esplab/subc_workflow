# SubX Diagnostics

This document tracks diagnostic utilities and how to run ad hoc checks safely.

## Diagnostic Utilities

- [utils/exceedance_diag.py](../utils/exceedance_diag.py): validates exceedance dimensionality and threshold selection behavior by model and region.
- [utils/split_percentiles_by_mmdd.py](../utils/split_percentiles_by_mmdd.py): utility to split large percentile climatology files into per-MMDD threshold files.

## Diagnostic Output Location

- Store ad hoc diagnostic logs under `logs/scratch/`.
- Avoid `/tmp` for workflow diagnostics.

## Typical Commands

Run exceedance diagnostics (main env):

```bash
PYTHONPATH=. conda run -n subc_workflow_env \
  python utils/exceedance_diag.py > logs/scratch/exceedance_diag.log 2>&1
```

Run non-PyCPT stage regression for a known date (main env):

```bash
PYTHONPATH=. conda run -n subc_workflow_env \
  python runners/cli.py --system subx --config config.yaml --init 20260305 \
  --stages ingest products > logs/scratch/regression_20260305.log 2>&1
```

Run the full pipeline including `pycpt` (both envs, via runner):

```bash
# The runner dispatches the pycpt stage through SUBC_PYCPT_ENV automatically.
SUBC_PYCPT_ENV=subc_pycpt_2_8_2 conda run -n subc_workflow_env \
  python runners/cli.py --system subx --config config.yaml --init 20260305 \
  --stages ingest products pycpt > logs/scratch/regression_full_20260305.log 2>&1
```

Run PyCPT standalone (pycpt env):

```bash
conda run -n subc_pycpt_2_8_2 \
  python -c "import pycpt, cptcore, cptio; print('ok')"
```