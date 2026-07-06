# SubX Workflow — Output Files Reference

This document describes every file produced by the subc_workflow pipeline, organized by stage. All paths reference defaults from `config.yaml`; override keys are noted where applicable.

---

## Output Directory Structure

```
/data/esplab/subc-backup/                          ← ingest (raw downloads)
    {GROUP}-{MODEL}/forecast/{VAR}/
        {VAR}_{GROUP}-{MODEL}_{YYYYMMDD}.daily.nc

/data/esplab/shared/model/initialized/subx/forecast/weekly/   ← out_weekly
    {FCSTDATE}/
        data/
            validation_manifest.json               ← preprocess
            subx_mme_anoms_wk_1-4_{FCSTDATE}.nc    ← products (main file)
            {PREFIX}{DOMAIN}Week{1-4}.nc            ← products (regional)
            {PREFIX}{DOMAIN}Weeks34.nc              ← products (regional agg)
            fcst_{FCSTDATE}.anom.pr_sfc.{stat}.nc  ← products (legacy)
            exceed_SUBC-MME_{VAR}_{REGION}_{FCSTDATE}_wk{W}.nc  ← products (pooled multi-model exceedance)
            manifest.json                          ← products
        images/
            {PREFIX}{DOMAIN}Week{1-4}.png          ← products (panel plots)
            {PREFIX}{DOMAIN}Weeks34.png
            {REGION}/
                exceed_mme_{VAR}_{REGION}_{FCSTDATE}_wk{W}.png
    pycpt/                                         ← pycpt_case_root
        {FCSTDATE}/pycpt/{REGION}/
            data/   ← case NetCDFs
            plots/  ← RPSS and bias-correction PNGs

somclass23:/home/kpegion/http/subc/forecasts/      ← publish (web server)
    images/{FCSTDATE}/  ← all PNGs
    images/Latest/      ← copy of most recent PNGs
    data/{FCSTDATE}/    ← all NetCDFs
    index.html          ← updated date dropdown
```

---

## Stage 1: Ingest

Downloads raw SubX forecast files from the IRI Data Library (IRIDL).

**Output directory:** `{rt_root}/{GROUP}-{MODEL}/{datatype}/{VAR}/`  
Default `rt_root`: `/data/esplab/subc-backup/`

| File pattern | Format | Content |
|---|---|---|
| `{VAR}_{GROUP}-{MODEL}_{YYYYMMDD}.daily.nc` | NetCDF4 | Raw daily forecast for one model, variable, and initialization date. Dimensions: S (start time), M (ensemble members), L (lead days), Y (lat), X (lon), optionally P (pressure level). |

**Notes:**
- One file per initialization date per variable per model.
- Downloads limited to files within a configurable lookback window (default: last 7 days for realtime runs).
- CFSv2 files contain all four daily start times (00z, 06z, 12z, 18z) merged into one file per calendar date.

---

## Stage 2: Preprocess

Validates that expected realtime input files are present before product generation.

**Output directory:** `{out_weekly}/{FCSTDATE}/data/`

| File | Format | Content |
|---|---|---|
| `validation_manifest.json` | JSON | Per model/variable validation records: group, model, variable, file pattern searched, matched file paths, and pass/fail status. |

**Notes:**
- Missing files produce warnings, not errors (policy: `warn_on_missing` in `config.yaml`).
- This file is informational; downstream stages use ingest files directly.

---

## Stage 3: Products

The main product generation stage. Produces all scientific data files and visualizations.

**Output directory:** `{out_weekly}/{FCSTDATE}/`

### Data files (`data/`)

| File pattern | Format | Content |
|---|---|---|
| `subx_mme_anoms_wk_1-4_{FCSTDATE}.nc` | NetCDF4 | **Main output file.** Weekly anomalies for weeks 1–4, all configured models plus SUBC-MME (multi-model ensemble mean). Contains ensemble member arrays and ensemble means per variable. |
| `{PREFIX}{DOMAIN}Week{1–4}.nc` | NetCDF4 | Regional weekly anomaly data, one file per variable prefix, domain, and week. |
| `{PREFIX}{DOMAIN}Weeks34.nc` | NetCDF4 | Weeks 3–4 aggregated (sum for precipitation, mean for other variables). |
| `fcst_{FCSTDATE}.anom.pr_sfc.emin.nc` | NetCDF4 | Legacy: precipitation anomaly ensemble minimum across models. |
| `fcst_{FCSTDATE}.anom.pr_sfc.emean.nc` | NetCDF4 | Legacy: precipitation anomaly ensemble mean across models. |
| `fcst_{FCSTDATE}.anom.pr_sfc.emax.nc` | NetCDF4 | Legacy: precipitation anomaly ensemble maximum across models. |
| `exceed_SUBC-MME_{VAR}_{REGION}_{FCSTDATE}_wk{W}.nc` | NetCDF4 | Pooled multi-model-ensemble exceedance probability grid: every configured model's ensemble members are pooled together, then the fraction exceeding the configured percentile threshold (default: 95th) during forecast week W is computed for the given region. One file per region/week; no per-model exceedance files are produced. |
| `manifest.json` | JSON | Run metadata: forecast date, models used, variables, weeks, regions processed, file counts, creation timestamp. |

**Variable prefixes used in regional filenames:**

| Prefix | Variable | Units |
|---|---|---|
| `2mTemp` | 2m temperature anomaly | K |
| `Precip` | Precipitation anomaly | mm/week |
| `500hPaGeopotentialHeight` | 500 hPa geopotential height anomaly | m |
| `SfcTemp` | Surface temperature anomaly | K |
| `LongwaveAtToa` | OLR anomaly (rlut) | W/m² |
| `MSLP` | Mean sea level pressure anomaly | Pa |

**Domains used in regional filenames:** `Global`, `NorthAmerica`, `Venezuela`, `Iran`, `Mexico`, `NorthernHemisphere` (zg only)

### Image files (`images/`)

All images are PNG at 150 dpi.

| File pattern | Content |
|---|---|
| `{PREFIX}{DOMAIN}Week{1–4}.png` | Multi-model panel plot of weekly anomalies for the given variable and domain. Each panel shows one model; final panel shows the SUBC-MME. |
| `{PREFIX}{DOMAIN}Weeks34.png` | Same layout, weeks 3–4 aggregated. |
| `{REGION}/exceed_mme_{VAR}_{REGION}_{FCSTDATE}_wk{W}.png` | Single pooled multi-model-ensemble exceedance probability map for the given variable, region, and week. Stored in a per-region subdirectory. |

---

## Stage 4: ArrayLake

Syncs forecast data to the public ArrayLake cloud repository for remote access without requiring local files.

**Destination:** ArrayLake repository `ou-subc/subc-forecasts`, branch `main`

**Data organization:**

| ArrayLake group | Content |
|---|---|
| `{group}-{model}-forecast` | All forecast variables for one model. Zarr format. Dimensions: S (init date), M (member), L (lead day), Y (lat), X (lon), optionally P (pressure). |

**Variables synced:** `pr`, `tas`, `rlut`, `ts`, `ua`, `va`, `zg` (configurable via `arraylake.variables`)  
**Skipped models:** configurable via `arraylake.skip_models` (default: `NCEP-CFSv2`)

**Notes:**
- Only new initialization dates are appended on each run; existing data is not rewritten.
- Public read access requires no API token (see `arraylake/read_subc_arraylake_public.ipynb`).

---

## Stage 5: PyCPT

Runs CPT-based probabilistic post-processing for configured regions.

**Output root:** `{pycpt_case_root}/{FCSTDATE}/pycpt/{REGION}/`  
Default `pycpt_case_root`: `{out_weekly}/pycpt/`

| Location | File pattern | Format | Content |
|---|---|---|---|
| `data/` | `{PREDICTOR}-{LEAD_LOW}-{LEAD_HIGH}.nc` | NetCDF4 | Hindcast predictor/predictand weekly files used for CPT training. |
| `data/` | `{PREDICTOR}-{YYYY}-{M}-{D}-L{LOW}-{HIGH}.nc` | NetCDF4 | Forecast predictor file for this initialization date. |
| `plots/` | `pycpt_rpss_w{W}_{REGION}_{SEASON}_{FCSTDATE}.png` | PNG | RPSS (Ranked Probability Skill Score) diagnostic map per week. |
| `plots/` | `bc_week{W}_{MODEL}_{REGION}_{FCSTDATE}.png` | PNG | Bias-corrected forecast plot showing before/after CPT calibration. |

**Configured regions:** `Venezuela`, `Iran`, `Mexico` (from `pycpt_regions` in `config.yaml`)

---

## Stage 6: Publish

Transfers products to the web server for public access.

**Destination host:** `somclass23` (override: `SUBX_WEB_HOST`)  
**Destination base:** `/home/kpegion/http/subc/forecasts/` (override: `SUBX_WEB_BASE`)

| Remote path | Content |
|---|---|
| `images/{FCSTDATE}/` | All PNG files from `{out_weekly}/{FCSTDATE}/images/` |
| `data/{FCSTDATE}/` | All NetCDF and JSON files from `{out_weekly}/{FCSTDATE}/data/` |
| `images/Latest/` | Copy of the most recent forecast's images (updated each run) |
| `index.html` | Web page date dropdown updated to include the new forecast date |

**Notes:**
- An optional subdirectory can be used for test publishes via `SUBX_PUBLISH_SUBDIR` (e.g., `test`).
- Remote file counts are verified after transfer by default (`SUBX_PUBLISH_VERIFY_REMOTE=1`).

---

## Naming Conventions

| Component | Pattern | Example |
|---|---|---|
| Forecast date | `YYYYMMDD` (Thursday) | `20260528` |
| Week number | `Week1` – `Week4`, `Weeks34` | `Week2`, `Weeks34` |
| Region name | Full name, no spaces | `NorthAmerica`, `Venezuela` |
| Variable code | Short lowercase | `pr`, `tas`, `zg`, `rlut` |
| Model ID | `{GROUP}-{MODEL}` | `EMC-GEFSv12_CPC`, `ECCC-GEPS8` |
| MME label | `SUBC-MME` | `SUBC-MME` |
| Ensemble stats | `emin`, `emean`, `emax` | `fcst_20260528.anom.pr_sfc.emean.nc` |
| Ingest file | `{VAR}_{GROUP}-{MODEL}_{YYYYMMDD}.daily.nc` | `pr_EMC-GEFSv12_CPC_20260528.daily.nc` |

---

## File Format Reference

| Format | Extension | Used for |
|---|---|---|
| NetCDF4 | `.nc` | All scientific data grids (anomalies, exceedance probabilities, raw forecasts) |
| PNG | `.png` | All visualizations (150 dpi) |
| JSON | `.json` | Manifests and validation reports |
| Zarr | (cloud) | ArrayLake cloud storage |

---

## Configuration Keys

The following `config.yaml` keys control output paths:

| Key | Default value | Controls |
|---|---|---|
| `paths.rt_root` | `/data/esplab/subc-backup` | Ingest output root |
| `paths.out_weekly` | `/data/esplab/shared/model/initialized/subx/forecast/weekly` | Products, preprocess, and publish source root |
| `paths.pycpt_case_root` | `{out_weekly}/pycpt` | PyCPT output root |
| `arraylake.repo` | `ou-subc/subc-forecasts` | ArrayLake repository |
| `arraylake.variables` | `[pr, tas, rlut, ts, ua, va, zg]` | Variables synced to ArrayLake |
| `arraylake.skip_models` | `[NCEP-CFSv2]` | Models excluded from ArrayLake |
| `exceedance.percentile` | `95` | Threshold for exceedance probability files |
| `exceedance.window_days` | `7` | Rolling window length (days) for the exceedance calculation |
| `pycpt_regions` | `[Venezuela, Iran, Mexico]` | Regions processed by PyCPT stage |
