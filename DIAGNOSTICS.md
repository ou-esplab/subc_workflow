# SubX Diagnostics

This document tracks diagnostic utilities and how to run ad hoc checks safely.

## Diagnostic Utilities

- [utils/exceedance_diag.py](utils/exceedance_diag.py): validates exceedance dimensionality and threshold selection behavior by model and region.
- [utils/split_percentiles_by_mmdd.py](utils/split_percentiles_by_mmdd.py): utility to split large percentile climatology files into per-MMDD threshold files.

## Diagnostic Output Location

- Store ad hoc diagnostic logs under `logs/scratch/`.
- Avoid `/tmp` for workflow diagnostics.

## Typical Commands

Run exceedance diagnostics:

```bash
PYTHONPATH=. ~/miniconda3/bin/conda run -n subc_workflow_env \
  python utils/exceedance_diag.py > logs/scratch/exceedance_diag.log 2>&1
```

Run full workflow regression for a known date:

```bash
PYTHONPATH=. ~/miniconda3/bin/conda run -n subc_workflow_env \
  python runners/cli.py --system subx --config config.yaml --init 20260305 \
  --stages ingest products pycpt > logs/scratch/regression_full_20260305.log 2>&1
```