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
- 2026-07-14: Added real-time direct-download ingest for GMAO GEOS_V3 (`direct_gmao_v3`) — regrids the native 0.5-deg grid to the standard 1-deg SubX grid, discovers ensemble member count per date (5 normally, 15 on month-end dates), and renames GMAO's raw in-file diagnostic names (PRECTOTCORR/T2M/OLR/TS/U/V/H) to SubX var names. Runs alongside the existing GMAO-GEOS_V2p1_5daily; not yet in ArrayLake (`arraylake.skip_models`). Supersedes the 2026-06-02 attempt, which was reverted for being incomplete.
- 2026-08-31: Added East Africa region (28–52°E, 12°S–15°N) with PyCPT bias-correction enabled.
- 2026-09-04: Added a `gdex` pipeline stage that syncs raw SubX model files to a public NCAR GDEX mirror over Globus (new `gdex/` module, mirroring `arraylake/`'s design), plus a standalone one-time hindcast backfill script (supports a `--limit N` small-batch test mode, since every model's hindcast archive runs into the hundreds of GB). Disabled by default (`gdex.enabled: false`) pending the real NCAR GDEX Globus collection UUID and a one-time `globus login`. `globus-cli` runs from its own dedicated `subc_globus_env` conda env (`SUBC_GDEX_ENV`), not `subc_workflow_env`, after discovering `subc_workflow_env`'s Python `sqlite3` module has been broken since 2026-05-07 (pre-existing `sqlite`/`libsqlite` conda package conflict, unrelated to this change) and `globus-cli` needs it for OAuth token caching.
- 2026-10-27: Retired GMAO GEOS_V2p1_5daily (GMAO ended V2.1) and switched fully to GEOS_V3: V2 removed from `config.yaml` (models, name map, ingest/shadow sources), products ordering and the static climatology list; its local/Arraylake/GDEX archive is kept frozen. V3 now has an Arraylake group (`gmao-geos_v3-forecast`, L capped to 45 leads, M padded to 15), and its real-time `L` is labelled by step (0.5-based) for every variable. V3 products remain pr/tas only (hindcast portal limit).

## Diagnostics

- Diagnostic utilities and ad hoc debug workflows are documented in [docs/DIAGNOSTICS.md](docs/DIAGNOSTICS.md).
