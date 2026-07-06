# SubX Weekly Forecast Workflow - Design Documentation

This document describes the architecture and design intent of the SubX workflow.

---

## Workflow Stages

```text
Realtime SubX Inputs
   ↓
Arraylake Append (Optional)
   ↓
Validation
   ↓
Weekly Products and Exceedance
   ↓
Regional PyCPT Post-Processing
   ↓
Publish
```

Primary entrypoint:

- `python3 runners/cli.py --system subx --config config.yaml --init YYYYMMDD`

Legacy wrapper:

- `./subx_pipeline.sh YYYYMMDD config.yaml`

The shell wrapper delegates to [runners/cli.py](../runners/cli.py) and is not the authoritative implementation.

---

## Design Principles

- Keep the Python runner as the single orchestration entrypoint.
- Make stage behavior explicit and configuration-driven.
- Prefer local archive compatibility over hard-coded remote assumptions.
- Keep exceedance logic strict about spatial dimensionality.
- Avoid silently averaging away unexpected dimensions.
- Produce one authoritative pooled multi-model-ensemble (MME) output per product/region/week rather than N per-model variants, to keep on-disk state unambiguous.

---

## Ingest Design

- Ingest is controlled by runner stage selection.
- Download execution is delegated externally through `SUBX_DOWNLOAD_BIN`.
- Smoke-test request generation is supported with `SUBX_DOWNLOAD_STUB=1`.
- The workflow does not embed a guessed operational download endpoint.
- Direct provider downloaders (ESRL, GMAO, ECCC, RSMAS, and CFS/GEFS mirrors) are implemented in [utils/download_subx_direct.py](../utils/download_subx_direct.py) as alternatives to IRIDL, selected per-model via `ingest.model_source` in [config.yaml](../config.yaml).
- RSMAS publishes 9 ensemble members per date; the direct downloader fetches and stacks all 9 (a per-source bug previously kept only 1).
- ECCC per-level tar selection uses `ingest.direct.eccc.var_levels` to pick the configured pressure level (e.g. 200/500 mb) instead of falling back to alphabetical file ordering, and embeds a proper size-1 `P` coordinate for the selected level.

---

## Validation Design

- [preprocess/validate_realtime.py](../preprocess/validate_realtime.py) performs preflight checks before products are built.
- Validation is configuration-driven by model list, variables, and expected file layout.
- The validation stage writes manifests into the run output directory.
- `validation.policy` determines whether missing inputs are fatal.

---

## Products Design

Weekly product generation is implemented in [products/forecast.py](../products/forecast.py).

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

Shared exceedance utilities live in [utils/exceedance_utils.py](../utils/exceedance_utils.py) (date/xarray helpers unrelated to exceedance remain in [utils/subc_pycpt_utils.py](../utils/subc_pycpt_utils.py): `latest_thursday`, `fcst_week_dates`, `ensure_lon`, `safe_concat`, `weekly_reduce`, `save_manifest`).

Design intent:

- Use model-specific percentile threshold files instead of a single generic threshold source.
- Normalize threshold coordinate names so forecast and threshold grids can be aligned safely.
- Compute exceedance probabilities from rolling windows along lead time.
- Require summary outputs to remain strict 2D lat/lon maps for plotting.
- Skip plotting when outputs are not map-like instead of collapsing dimensions implicitly.
- Produce a single pooled multi-model-ensemble (MME) exceedance map per region/week — not per-model maps. Pooling combines every configured model's raw exceedance-member counts and ensemble sizes (sum counts across models, sum member counts across models, then divide once), which is methodologically distinct from averaging independently-computed per-model probabilities.
- Fix the seam artifact in Global (Robinson-projection) exceedance maps by closing the 0–359° longitude grid with `cartopy.util.add_cyclic_point` before contouring.

Threshold selection behavior:

- Prefer exact MMDD threshold files.
- Allow nearest-MMDD fallback only within `exceedance.max_fallback_days`.
- Skip a model entirely when no threshold falls within that bound (it simply doesn't contribute to the pooled MME for that date).

---

## PyCPT Design

PyCPT execution is orchestrated through the runner and shell bridge, with implementation centered in [postprocess/pycpt_s2s_realtime.py](../postprocess/pycpt_s2s_realtime.py) and shared helpers.

Supported modes:

- Standard download-backed PyCPT runs
- Local staged-data mode
- Local SubX archive predictor generation
- Config-driven or CLI-driven predictor model groups
- Regional parallel execution

Model selection precedence is explicit and shared between configuration and CLI.

---

## Arraylake Design

Arraylake append execution is orchestrated through the runner stage `arraylake` and implemented by [arraylake/run_addvars_rt.sh](../arraylake/run_addvars_rt.sh) and [arraylake/update_arraylake_fcsts.py](../arraylake/update_arraylake_fcsts.py).

[arraylake/addvars_rt.py](../arraylake/addvars_rt.py) is retained only as a deprecated legacy reference and is not invoked by the current workflow runner.

Design intent:

- Keep Arraylake integration optional and configuration-driven via `arraylake.enabled` in [config.yaml](../config.yaml).
- Use workflow model configuration as the source of truth for group/model iteration.
- Append only new start dates (`S`) to existing repository groups.
- Avoid hard failures for missing model-variable files: when a source file is missing for a configured variable on a new date, NaN-fill that variable's slot instead of holding back the whole date, so every configured variable's `S`-dimension grows together on every run.
- Run a separate backfill-check pass ("Section 4" in `update_arraylake_fcsts.py`) on recent existing dates that overwrites NaN slots with real data via a raw zarr region write, once a source file becomes available locally — this keeps the group current without requiring a full re-append.
- A one-time repair script, [arraylake/repair_partial_variable_gaps.py](../arraylake/repair_partial_variable_gaps.py), corrected groups desynced by the pre-fix behavior (historical/one-time use).
- Load `ARRAYLAKE_TOKEN` from `arraylake/.env` at runtime.
- Keep Arraylake as a sink stage; downstream stages do not read Arraylake outputs.

---

## Publish Design

The `publish` stage is responsible for syncing final workflow outputs for downstream web and stakeholder access.

- The web frontend [publish/web/index.html](../publish/web/index.html) is version-controlled in this repo as the source of truth; the publish stage updates the local tracked copy's date dropdown, then deploys it to the remote host on every run instead of editing the remote file in place — this prevents drift from ad hoc SSH edits to the live site.
- Destination host selection prefers `somclass22` and falls back to `somclass23` (same NFS home) if unreachable, unless `SUBX_WEB_HOST` is set explicitly.

---

## Code Organization

- [runners/cli.py](../runners/cli.py): canonical workflow runner
- [products/forecast.py](../products/forecast.py): weekly products and exceedance generation
- [preprocess/validate_realtime.py](../preprocess/validate_realtime.py): preflight validation
- [postprocess/pycpt_s2s_realtime.py](../postprocess/pycpt_s2s_realtime.py): regional PyCPT execution
- [utils/subc_pycpt_utils.py](../utils/subc_pycpt_utils.py): shared date, xarray, and manifest helpers (`latest_thursday`, `fcst_week_dates`, `ensure_lon`, `safe_concat`, `weekly_reduce`, `save_manifest`)
- [utils/exceedance_utils.py](../utils/exceedance_utils.py): exceedance/threshold helpers (`compute_exceedance`, `nearest_mmdd_threshold`, `plot_exceedance_summary`, `plot_exceedance_panels`)
- [utils/download_subx_direct.py](../utils/download_subx_direct.py): direct-provider ingest downloaders (ESRL, GMAO, ECCC, RSMAS, CFS/GEFS mirrors)
- [utils/split_percentiles_by_mmdd.py](../utils/split_percentiles_by_mmdd.py): threshold file splitter utility
- [utils/exceedance_diag.py](../utils/exceedance_diag.py): dimensional and threshold-selection diagnostics

---

## Operational Notes

- Runtime outputs and logs are date-scoped and stage-scoped.
- Scratch diagnostics should be written under `logs/scratch/` rather than `/tmp`.
- The workflow may intentionally tolerate missing models when configured data are absent, but that tolerance is explicit and stage-dependent.

---

## Rationale

This design prioritizes reproducibility, explicitness, and operational clarity over hidden convenience behavior. The workflow should make it obvious which models ran, which were skipped, why they were skipped, and which outputs are authoritative for a given forecast date.