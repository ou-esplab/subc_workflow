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
- Forecast products guide (for users interpreting output maps/files): [docs/products_outputs.md](docs/products_outputs.md)

## Current Notes

- Exceedance thresholds are selected per model from per-MMDD files.
- Nearest-MMDD fallback is only allowed within `+/- 7` days by default via `exceedance.max_fallback_days` in [config.yaml](config.yaml).
- If no threshold is close enough, that model is excluded from the pooled MME exceedance calculation for that date/region/week (no per-model files exist to remove).
- Exceedance now produces a single pooled multi-model-ensemble (MME) map per region/week (`exceed_SUBC-MME_*.nc` / `exceed_mme_*.png`); no per-model exceedance outputs are produced.

## Recent Changes

- 2026-03-29: Exceedance processing now enforces bounded threshold fallback (`max_fallback_days`) and removes stale model outputs when a model is skipped.
- 2026-06-02: Added GMAO GEOS_V3 ingest support.
- 2026-07-02: Added direct-download providers for RSMAS, CFS/GEFS, GMAO, and ESRL; fixed RSMAS to fetch all 9 ensemble members.
- 2026-07-03: Replaced ArrayLake's hold-back-whole-date gate with NaN-fill-on-append plus a backfill-check pass; fixed ECCC per-level pressure selection and P-dimension embedding.
- 2026-07-05: Added a daily `ingest arraylake` cron entry (06:00 UTC) to keep ArrayLake near-real-time between weekly runs.
- 2026-07-06: Redesigned exceedance to produce a single pooled MME map per region/week (removed per-model exceedance outputs); fixed a Global-map seam bug; version-controlled `publish/web/index.html` as the deploy source of truth.

## Diagnostics

- Diagnostic utilities and ad hoc debug workflows are documented in [docs/DIAGNOSTICS.md](docs/DIAGNOSTICS.md).
