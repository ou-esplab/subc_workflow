# SubX Unified Workflow

This repository contains the weekly SubX ingest, validation, product-generation, and PyCPT post-processing workflow.

## Environment

Create the workflow environment with:

```bash
conda env create -f environment.yml
conda activate subc_workflow_env
```

The environment spec in [environment.yml](environment.yml) is based on the working `nmme_workflow_env` package set and pins the key PyCPT and analysis dependencies used by this workflow.

## Entry Points

Run the full workflow through the Python runner. This is the canonical entrypoint for the SubX workflow:

```bash
python3 runners/cli.py --system subx --config config.yaml --init YYYYMMDD
```

Runner options aligned with the NMME workflow:

- Dry-run products stage:

```bash
python3 runners/cli.py --system subx --config config.yaml --init YYYYMMDD --stages products --products-dry-run
```

- Run PyCPT only for selected regions:

```bash
python3 runners/cli.py --system subx --config config.yaml --init YYYYMMDD --stages pycpt --pycpt-only Mexico Iran
```

- Run PyCPT in dry-run mode (data prep only, skip CPT eval):

```bash
python3 runners/cli.py --system subx --config config.yaml --init YYYYMMDD --stages pycpt --pycpt-dry-run
```

- Run regional PyCPT jobs in parallel:

```bash
python3 runners/cli.py --system subx --config config.yaml --init YYYYMMDD --stages pycpt --pycpt-max-workers 3
```

Run the shell wrapper only when you want environment activation handled before calling the runner:

```bash
./subx_pipeline.sh YYYYMMDD config.yaml
```

`subx_pipeline.sh` is just a wrapper around `runners/cli.py`; it is not the primary workflow implementation.

The stages are:

- `ingest`: download or request realtime SubX files.
- `products`: validate inputs, build weekly forecast products, and write manifests.
- `pycpt`: run the PyCPT regional post-processing step.

Logs are written under `logs/YYYYMMDD_HHMMSS/subx/<init>/` because both entrypaths now route through the runner.

## Configuration Notes

- `model_name_map` is applied to local on-disk model names. Use it when the upstream model identifier differs from your local realtime and climatology directory layout.
- `validation.policy` controls whether missing realtime inputs stop the run or only emit warnings.
- `concurrency.downloads` controls how many downloader processes run at once during ingest.
- `email.enabled` controls best-effort failure notifications.

## Download Behavior

The downloader script is now explicit about its operating mode.

- For production, export `SUBX_DOWNLOAD_BIN` to point at an executable that performs the real download and writes files into `paths.rt_root`.
- For workflow smoke tests, export `SUBX_DOWNLOAD_STUB=1` to create request-marker files instead of contacting a remote server.

The repository does not guess a remote endpoint anymore because the previous downloader implementation was incomplete and inconsistent with the configured local file layout.

## Validation

`validate_realtime.py` performs a preflight check before forecast generation. It verifies that each configured model-variable combination has at least one matching realtime file for the requested forecast date and writes a validation manifest into the stage output directory.

## Products Smoke Mode

When you need a workflow dry run without real forecast NetCDF files, enable products smoke mode:

- Set `SUBX_PRODUCTS_SMOKE=1` in the environment, or
- Set `workflow.products_smoke: true` in your config.

In this mode, the products stage still runs validation and writes `manifest.json`, but it does not fail if no realtime model inputs are found.

## PyCPT Local Data Mode

You can run PyCPT from local pre-staged files instead of downloading from IRIDL.

Add this under `pycpt` in config:

```yaml
pycpt:
	training_season: Feb-Apr
	use_local_data: true
	local_data_dir: /path/to/pycpt/data
	local_data_strict: true
	force_download: false
	target: Mar
```

Notes:

- `local_data_dir` should contain files named like:
	- `GEFSv12.PRCP-1-7.nc`
	- `GEFSv12.PRCP-YYYY-M-D-L1-7.nc`
	- `CHIRPS.PRCP-1-7.nc`
	for each configured week lead.
- With `local_data_strict: true`, the run fails fast if any required local file is missing.
- With `local_data_strict: false`, PyCPT can still download missing files unless all expected files are present.

### Local SubX Archive Mode

If your predictor data are in the SubX archive layout:

`<root>/<GROUP>-<MODEL>/<forecast,hindcast>/<var>/<var>_<GROUP>-<MODEL>_YYYYMMDD.daily.nc`

you can enable direct local predictor generation for PyCPT:

```yaml
pycpt:
	use_local_subx_predictor: true
	local_subx_root: /data/esplab/subc-backup
	local_subx_model_id: EMC-GEFSv12_CPC
```

This mode builds PyCPT-compatible predictor weekly files from local SubX forecast/hindcast NetCDFs and avoids predictor downloads. Observed predictand downloads may still occur unless those files are also pre-staged.

## Tests

Run the unit test suite with:

```bash
python3 -m unittest discover -s tests -v
```

The tests cover workflow orchestration, validation behavior, helper utilities, and the downloader stub path.
