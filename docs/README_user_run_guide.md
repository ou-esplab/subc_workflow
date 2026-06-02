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
2. Both conda environments are installed (see below).

### Two-Environment Setup

This workflow uses two separate conda environments:

| Environment | File | Used by |
|---|---|---|
| `subc_workflow_env` | [environment.yml](../environment.yml) | All stages except `pycpt` |
| `subc_pycpt_2_8_2` | [environment.pycpt-2.8.2.yml](../environment.pycpt-2.8.2.yml) | `pycpt` stage only |

Create both:

```bash
conda env create -f environment.yml
conda env create -f environment.pycpt-2.8.2.yml
```

Verify the main environment:

```bash
conda run -n subc_workflow_env python -c "import xarray, pandas, yaml; print('ok')"
```

Verify the PyCPT environment:

```bash
conda run -n subc_pycpt_2_8_2 python -c "import pycpt, cptcore, cptio; print('ok')"
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

Run Arraylake in dry-run mode:

```bash
python3 runners/cli.py --system subx --config config.yaml --init 20260305 --stages arraylake --arraylake-dry-run
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
- Supports source routing from config so selected models can use direct
  provider endpoints while others continue using IRIDL.

Example source routing in `config.yaml`:

```yaml
ingest:
  source_default: iridl
  model_source:
    ESRL-FIMr1p1: direct_esrl
    GMAO-GEOS_V2p1_5daily: direct_gmao
    ECCC-GEPS8: direct_eccc
  direct:
    ftp_email: your_email@example.org
    providers:
      esrl:
        url: ftp://gsdftp.fsl.noaa.gov/SubX-ESRL-FIMr1.1/
      gmao:
        url: https://gmao.gsfc.nasa.gov/gmaoftp/gmaofcst/subx/GEOS_S2S_V2.1_fcst/
      eccc:
        url: https://collaboration.cmc.ec.gc.ca/cmc/CMOI/GRIB/GEPS/forecast/subX_fcst/
```

Supported source values: `iridl`, `direct`, `direct_esrl`, `direct_gmao`, `direct_eccc`.

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
- Legacy note: [arraylake/addvars_rt.py](../arraylake/addvars_rt.py) is retained only as a deprecated reference and is not part of the live workflow path.
- Only runs when `arraylake.enabled: true` in [config.yaml](../config.yaml).
- Loads `ARRAYLAKE_TOKEN` from [arraylake/.env](../arraylake/.env) when present.
- Supports `--arraylake-dry-run` to scan pending updates without writing to ArrayLake.
- No downstream stage (`preprocess`, `products`, `pycpt`, `publish`) depends on Arraylake outputs.

ArrayLake variable selection behavior:

- Global defaults come from `arraylake.variables`.
- Stage-specific overrides come from `arraylake.model_vars` when present.
- Per-model overrides come from `models[].vars`.
- Precedence: `arraylake.model_vars` overrides `models[].vars`, which overrides `arraylake.variables` for ArrayLake.
- Validation policy: unknown values in `models[].vars` are warned and skipped.
- If a model override resolves to no valid variables, that model is skipped with a warning.
- If `models[].vars` is omitted, the model uses the global `arraylake.variables` list.

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