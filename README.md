# SubX Unified Workflow

## Environment Setup

This workflow uses two conda environments:

| Environment | File | Purpose |
|---|---|---|
| `subc_workflow_env` | [environment.yml](environment.yml) | All stages except `pycpt` |
| `subc_pycpt_2_8_2` | [environment.pycpt-2.8.2.yml](environment.pycpt-2.8.2.yml) | `pycpt` stage only |

Create both environments:

```bash
conda env create -f environment.yml
conda env create -f environment.pycpt-2.8.2.yml
```

To update an existing environment after changes to its file:

```bash
conda env update --name subc_workflow_env --file environment.yml --prune --solver libmamba
conda env update --name subc_pycpt_2_8_2 --file environment.pycpt-2.8.2.yml --prune --solver libmamba
```

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
