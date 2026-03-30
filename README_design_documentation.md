# SubX Weekly Forecast Workflow - Design Documentation

This document describes the architecture and design intent of the SubX workflow.

---

## Workflow Stages

```text
Realtime SubX Inputs
   ↓
Validation
   ↓
Weekly Products and Exceedance
   ↓
Regional PyCPT Post-Processing
```

Primary entrypoint:

- `python3 runners/cli.py --system subx --config config.yaml --init YYYYMMDD`

Legacy wrapper:

- `./subx_pipeline.sh YYYYMMDD config.yaml`

The shell wrapper delegates to [runners/cli.py](runners/cli.py) and is not the authoritative implementation.

---

## Design Principles

- Keep the Python runner as the single orchestration entrypoint.
- Make stage behavior explicit and configuration-driven.
- Prefer local archive compatibility over hard-coded remote assumptions.
- Keep exceedance logic strict about spatial dimensionality.
- Avoid silently averaging away unexpected dimensions.
- Remove stale outputs when model eligibility changes between runs.

---

## Ingest Design

- Ingest is controlled by runner stage selection.
- Download execution is delegated externally through `SUBX_DOWNLOAD_BIN`.
- Smoke-test request generation is supported with `SUBX_DOWNLOAD_STUB=1`.
- The workflow does not embed a guessed operational download endpoint.

---

## Validation Design

- [validate_realtime.py](validate_realtime.py) performs preflight checks before products are built.
- Validation is configuration-driven by model list, variables, and expected file layout.
- The validation stage writes manifests into the run output directory.
- `validation.policy` determines whether missing inputs are fatal.

---

## Products Design

Weekly product generation is implemented in [forecast.py](forecast.py).

Core behavior:

- Load realtime forecast files per configured model and variable.
- Select the most recent initialization within the weekly SubX forecast window.
- Convert each model to an ensemble-mean field for anomaly calculation.
- Compute anomalies against hindcast climatology.
- Aggregate to CPC weeks 1 to 4.
- Build `SUBC-MME` as the model mean of weekly anomalies.

Outputs include:

- Weekly anomaly NetCDFs
- Region-oriented figures
- Manifest metadata

---

## Exceedance Design

Shared exceedance utilities live in [subc_pycpt_utils.py](subc_pycpt_utils.py).

Design intent:

- Use model-specific percentile threshold files instead of a single generic threshold source.
- Normalize threshold coordinate names so forecast and threshold grids can be aligned safely.
- Compute exceedance probabilities from rolling windows along lead time.
- Require summary outputs to remain strict 2D lat/lon maps for plotting.
- Skip plotting when outputs are not map-like instead of collapsing dimensions implicitly.

Threshold selection behavior:

- Prefer exact MMDD threshold files.
- Allow nearest-MMDD fallback only within `exceedance.max_fallback_days`.
- Skip a model entirely when no threshold falls within that bound.
- Remove stale exceedance outputs for skipped models so on-disk state reflects current logic.

---

## PyCPT Design

PyCPT execution is orchestrated through the runner and shell bridge, with implementation centered in [pycpt_s2s_realtime.py](pycpt_s2s_realtime.py) and shared helpers.

Supported modes:

- Standard download-backed PyCPT runs
- Local staged-data mode
- Local SubX archive predictor generation
- Config-driven or CLI-driven predictor model groups
- Regional parallel execution

Model selection precedence is explicit and shared between configuration and CLI.

---

## Code Organization

- [runners/cli.py](runners/cli.py): canonical workflow runner
- [forecast.py](forecast.py): weekly products and exceedance generation
- [validate_realtime.py](validate_realtime.py): preflight validation
- [pycpt_s2s_realtime.py](pycpt_s2s_realtime.py): regional PyCPT execution
- [subc_pycpt_utils.py](subc_pycpt_utils.py): shared date, exceedance, plotting, and xarray helpers
- [utils/split_percentiles_by_mmdd.py](utils/split_percentiles_by_mmdd.py): threshold file splitter utility
- [utils/exceedance_diag.py](utils/exceedance_diag.py): dimensional and threshold-selection diagnostics

---

## Operational Notes

- Runtime outputs and logs are date-scoped and stage-scoped.
- Scratch diagnostics should be written under `logs/scratch/` rather than `/tmp`.
- The workflow may intentionally tolerate missing models when configured data are absent, but that tolerance is explicit and stage-dependent.

---

## Rationale

This design prioritizes reproducibility, explicitness, and operational clarity over hidden convenience behavior. The workflow should make it obvious which models ran, which were skipped, why they were skipped, and which outputs are authoritative for a given forecast date.