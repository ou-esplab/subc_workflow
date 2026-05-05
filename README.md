# SubX Unified Workflow

## Environment Setup

Create the workflow environment with:

```bash
conda env create -f environment.yml
conda activate subc_workflow_env
```

The environment spec in [environment.yml](environment.yml) is based on the working `nmme_workflow_env` package set and pins the key PyCPT and analysis dependencies used by this workflow.

## Run (using unified runner)

```bash
python3 runners/cli.py --system subx --config config.yaml --init YYYYMMDD
```

- Stages: `ingest`, `arraylake`, `preprocess`, `products`, `pycpt`, `publish`.
- Canonical entrypoint: [runners/cli.py](runners/cli.py)
- Legacy shell entry: [subx_pipeline.sh](subx_pipeline.sh) delegates to the runner.

Outputs and logs are written under `logs/YYYYMMDD_HHMMSS/subx/<init>/`.

## Documentation

- User/operator run instructions: [docs/README_user_run_guide.md](docs/README_user_run_guide.md)
- Workflow design and architecture: [docs/README_design_documentation.md](docs/README_design_documentation.md)

## Current Notes

- Exceedance thresholds are selected per model from per-MMDD files.
- Nearest-MMDD fallback is only allowed within `+/- 7` days by default via `exceedance.max_fallback_days` in [config.yaml](config.yaml).
- If no threshold is close enough, the model is skipped and stale exceedance outputs for that model/date are removed.

## Recent Changes

- 2026-03-29: Exceedance processing now enforces bounded threshold fallback (`max_fallback_days`) and removes stale model outputs when a model is skipped.

## Diagnostics

- Diagnostic utilities and ad hoc debug workflows are documented in [docs/DIAGNOSTICS.md](docs/DIAGNOSTICS.md).
