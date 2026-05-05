# SubX Weekly Forecast Workflow - User Run Guide

This document explains how to run the full SubX weekly forecast workflow from a user and operator perspective.

---

## What This Workflow Does

- Ingests or requests realtime SubX forecast files
- Validates expected realtime inputs before product generation
- Builds weekly anomaly products and regional plots
- Computes exceedance products from model-specific percentile thresholds
- Runs regional PyCPT post-processing

---

## Prerequisites

Before running this workflow, ensure:

1. Local realtime and hindcast data roots in [config.yaml](../config.yaml) point to valid SubX archive locations.
2. The workflow environment is installed.
3. PyCPT/CPT dependencies are available if you plan to run the `pycpt` stage.

Recommended environment setup:

```bash
conda env create -f environment.yml
conda activate subc_workflow_env
```

Verify the environment if you will run PyCPT:

```bash
which CPT.x
python -c "import cptcore, cptio; print('ok')"
```

---

## Basic Command

Preferred command:

```bash
python3 runners/cli.py --system subx --config config.yaml --init 20260305
```

This runs the default end-to-end pipeline stages: `ingest`, `arraylake`, `preprocess`, `products`, `pycpt`, and `publish`.

Legacy wrapper:

```bash
./subx_pipeline.sh 20260305 config.yaml
```

The shell wrapper is only for environment activation and delegation. The canonical implementation is in [runners/cli.py](../runners/cli.py).

---

## Common Run Patterns

Run only the products stage:

```bash
python3 runners/cli.py --system subx --config config.yaml --init 20260305 --stages products
```

Run Arraylake append stage directly after ingest:

```bash
python3 runners/cli.py --system subx --config config.yaml --init 20260305 --stages ingest arraylake
```

Dry-run the products stage:

```bash
python3 runners/cli.py --system subx --config config.yaml --init 20260305 --stages products --products-dry-run
```

Run only PyCPT for selected regions:

```bash
python3 runners/cli.py --system subx --config config.yaml --init 20260305 --stages pycpt --pycpt-only Mexico Iran
```

Run PyCPT in dry-run mode:

```bash
python3 runners/cli.py --system subx --config config.yaml --init 20260305 --stages pycpt --pycpt-dry-run
```

Run regional PyCPT jobs in parallel:

```bash
python3 runners/cli.py --system subx --config config.yaml --init 20260305 --stages pycpt --pycpt-max-workers 3
```

Override the PyCPT predictor model group from the command line:

```bash
python3 runners/cli.py --system subx --config config.yaml --init 20260305 --stages pycpt --pycpt-models EMC-GEFSv12_CPC NCEP-CFSv2
```

---

## Workflow Stages

### `ingest`

- Requests or downloads realtime SubX files.
- Uses `SUBX_DOWNLOAD_BIN` for real download execution.
- Can use `SUBX_DOWNLOAD_STUB=1` for smoke-test request markers.

### `products`

- Validates realtime inputs.
- Builds weekly anomaly outputs.
- Computes exceedance products by region and model.
- Writes manifests and summary plots.

### `pycpt`

- Builds PyCPT-compatible regional inputs.
- Supports local staged data and local SubX archive predictor generation.
- Can operate on configured model groups or CLI-selected models.

### `arraylake`

- Appends new realtime start dates into existing Arraylake groups.
- Script entrypoint: [arraylake/run_addvars_rt.sh](../arraylake/run_addvars_rt.sh) -> [arraylake/update_arraylake_fcsts.py](../arraylake/update_arraylake_fcsts.py).
- Only runs when `arraylake.enabled: true` in [config.yaml](../config.yaml).
- Loads `ARRAYLAKE_TOKEN` from [arraylake/.env](../arraylake/.env) when present.
- No downstream stage (`preprocess`, `products`, `pycpt`, `publish`) depends on Arraylake outputs.

---

## Exceedance Behavior

Exceedance configuration is controlled under `exceedance` in [config.yaml](../config.yaml).

Important behavior:

- Thresholds are read from per-model, per-MMDD percentile files.
- Exact MMDD matches are preferred.
- Nearest-MMDD fallback is allowed only within `+/- 7` days by default.
- If no threshold file is within that range, the model is skipped.
- If a model is skipped after a previous successful run, stale exceedance NetCDF and PNG outputs for that model/date are removed.

Example:

```yaml
exceedance:
  var: pr
  lev: sfc
  percentile: 95
  window_days: 7
  max_fallback_days: 7
```

---

## Validation and Smoke Modes

Validation is performed by [preprocess/validate_realtime.py](../preprocess/validate_realtime.py) before forecast generation.

Products smoke mode can be enabled when you want the stage to finish without realtime model inputs:

- Set `SUBX_PRODUCTS_SMOKE=1`, or
- Set `workflow.products_smoke: true` in config.

In smoke mode, the workflow still writes manifests and validation outputs but does not fail on empty product inputs.

---

## PyCPT Local Data Mode

You can run PyCPT from local staged files instead of downloading from IRIDL.

Example config:

```yaml
pycpt:
  training_season: Feb-Apr
  use_local_data: true
  local_data_dir: /path/to/pycpt/data
  local_data_strict: true
  force_download: false
  target: Mar
```

Expected local file patterns include names like:

- `GEFSv12.PRCP-1-7.nc`
- `GEFSv12.PRCP-YYYY-M-D-L1-7.nc`
- `CHIRPS.PRCP-1-7.nc`

With `local_data_strict: true`, the run fails if any required local file is missing.

### Local SubX Archive Mode

If predictors already exist in the SubX archive layout:

`<root>/<GROUP>-<MODEL>/<forecast,hindcast>/<var>/<var>_<GROUP>-<MODEL>_YYYYMMDD.daily.nc`

you can build PyCPT predictors directly from those files:

```yaml
pycpt:
  use_local_subx_predictor: true
  local_subx_root: /data/esplab/subc-backup
  local_subx_model_id: EMC-GEFSv12_CPC
```

---

## PyCPT Model Selection

You can configure PyCPT predictor models directly in [config.yaml](../config.yaml):

```yaml
pycpt:
  predictor_var: PRCP
  model_group: [EMC-GEFSv12_CPC, NCEP-CFSv2]
  predictor_name_map:
    EMC-GEFSv12_CPC: GEFSv12.PRCP
    NCEP-CFSv2: CFSv2.PRCP
```

Model selection precedence is:

1. CLI `--pycpt-models`
2. `pycpt.predictor_names`
3. `pycpt.model_group`
4. `pycpt.models`
5. `pycpt.local_subx_model_id`

---

## Tests

Run the unit test suite with:

```bash
python3 -m unittest discover -s tests -v
```

The tests cover workflow orchestration, validation behavior, helper utilities, and downloader stub behavior.