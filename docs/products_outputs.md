# SubX Forecast Products Guide

This guide describes the forecast products available from the SubX workflow and how to interpret them. It is written for people looking at the maps and files, not for people editing the pipeline code — for that, see [README_design_documentation.md](README_design_documentation.md).

---

## Overview

Forecasts are produced weekly, each Thursday, using the **SubX-C multi-model ensemble** — six subseasonal-to-seasonal (S2S) prediction models contributed by NOAA, NASA, Environment and Climate Change Canada, and university/lab partners. Combining multiple independent models into a **SUBC-MME** (multi-model ensemble) reduces the influence of any single model's biases and gives a more robust probabilistic picture of week-to-week conditions than any one model alone.

The workflow also maintains a public, cloud-hosted copy of the raw ensemble forecast data via an optional ArrayLake sync stage, so users who want the underlying gridded data — rather than the summary maps below — can access it directly without needing local file access. See [Reference Datasets](#reference-datasets) and [arraylake/read_subc_arraylake_public.ipynb](../arraylake/read_subc_arraylake_public.ipynb).

Products are available for:

- **Variables:** Precipitation (`pr`), 2-meter temperature (`tas`), surface temperature (`ts`), outgoing longwave radiation (`rlut`), 200 hPa zonal/meridional wind (`ua`, `va`), 500 hPa geopotential height (`zg`) — not every model provides every variable (see the [Models](#models) table below).
- **Regions:** Global, North America, Venezuela, Iran, Mexico, Central Asia.
- **Lead weeks:** Weeks 1–4 after each Thursday initialization, plus a combined Weeks 3–4 outlook.
- **Climatological baseline:** Each model's own hindcast climatology — models are not recalibrated to a shared observational baseline before combination (see [Reference Datasets](#reference-datasets)).

---

## Forecast Products

### 1. Anomaly Maps (primary forecast product)

**What it shows:** The forecast departure from each model's own climatology, for a given variable, region, and week — e.g. how much wetter/drier or warmer/cooler than normal conditions are expected.

**How to read it:** A grid of panels, one per configured model, plus a final `SUBC-MME` panel — the multi-model ensemble mean, computed as the straight average of each model's own weekly ensemble-mean anomaly. Warm colors/positive values indicate above-normal conditions (wetter, for precipitation); cool colors/negative values indicate below-normal conditions. Compare panels to see which models agree with the MME signal and which diverge — broad agreement across models is a stronger signal than the MME panel alone conveys.

**Available for:** All 6 configured variables (subject to per-model availability — see [Models](#models)), all 6 regions, Weeks 1–4 individually and a combined Weeks 3–4 aggregate (summed for precipitation, averaged for other variables).

**Format:** Multi-panel PNG (one panel per model plus MME), plus a companion NetCDF with per-model and MME arrays.

### 2. Exceedance Probability Maps (pooled multi-model ensemble)

Three exceedance products are computed, each a single map per region/week (not a per-model panel grid). All three use the same **combination method**: rather than averaging each model's independently-computed probability, every model's individual ensemble members are pooled into one large combined ensemble before the exceedance fraction is computed once. This is a **different combination method** than the Anomaly Map's MME (which averages each model's own mean). A model contributing more ensemble members (e.g. ECCC-GEPS8's 21 vs. GMAO's 4) therefore has proportionally more influence on the pooled result — this is a deliberate design choice, not an oversight.

**Format (all three):** Single-panel PNG per region/week; companion NetCDF with the pooled probability grid. Global maps use a Robinson projection.

#### 2a. Precip Exceedance (heavy rain)

**What it shows:** The probability that precipitation exceeds its climatological 95th percentile on **at least one day** during the forecast week, for a given region.

**How to read it:** Values are percentages: e.g. 40% at a grid point means 40% of the pooled ensemble members had at least one day that week exceeding that location's 95th-percentile threshold.

**Available for:** Precipitation, 95th percentile, per region, Weeks 1–4.

#### 2b. Temp Exceedance (extreme heat)

**What it shows:** The probability that 2m temperature (`tas`) exceeds its climatological 95th percentile on **at least one day** during the forecast week, for a given region — the extreme-heat analog of the precip product above.

**How to read it:** Same interpretation as the precip exceedance map, applied to temperature instead of precipitation.

**Available for:** 2m temperature, 95th percentile, per region, Weeks 1–4.

#### 2c. Precip Drought (dry spell)

**What it shows:** The probability that precipitation stays below its climatological 5th percentile on **every day** during the forecast week, for a given region — a sustained dry-spell signal.

**How to read it:** This product uses a different aggregation than the two above: low-percentile precipitation thresholds are often close to zero, so "at least one dry day" would trigger almost every week and wouldn't be a meaningful signal. Requiring **every day in the week** to fall below the threshold is closer to a meteorological drought/dry-spell definition.

**Available for:** Precipitation, 5th percentile, per region, Weeks 1–4.

> Variable/percentile/direction/aggregation for all three products are configurable via the `exceedance:` list in `config.yaml` (one entry per product).

> **Note on PyCPT:** This workflow includes an optional CPT-based bias-correction stage (`pycpt`), but it is disabled by default in the production cron schedule and is not part of the standard set of products served on this site. If enabled for specific regions in the future, bias-corrected products would be documented separately here.

---

## Regions

| Region | Coverage (lon °E / lat °N) |
|---|---|
| Global | 0–360°E, 90°S–90°N |
| NorthAmerica | 190–305°E, 15–75°N |
| Venezuela | 287–300°E, 0–13°N |
| Iran | 40–64°E, 24–40°N |
| Mexico | 242–263°E, 20–33°N |
| CAsia | 45–89°E, 29–56°N |

---

## Lead Weeks

Unlike seasonal forecast systems, SubX products are organized around **weekly** forecast windows following the CPC convention: each week runs Saturday–Friday, starting 2 days after the Thursday initialization date.

| Week label | Definition (relative to init Thursday) |
|---|---|
| Week1 | Sat–Fri, days 2–8 after init |
| Week2 | Sat–Fri, days 9–15 after init |
| Week3 | Sat–Fri, days 16–22 after init |
| Week4 | Sat–Fri, days 23–29 after init |
| Weeks34 | Combined Week3 + Week4 window (summed for precipitation, averaged otherwise) |

---

## Output Files

### NetCDF Data Files

Under `forecast/{FCSTDATE}/data/`:

| File | Contents |
|---|---|
| `subx_mme_anoms_wk_1-4_{FCSTDATE}.nc` | Main output: per-model and SUBC-MME weekly anomalies, weeks 1–4, all variables |
| `{Prefix}{Region}Week{1-4}.nc` | Regional weekly anomaly data for one variable/region/week |
| `{Prefix}{Region}Weeks34.nc` | Weeks 3–4 aggregate for one variable/region |
| `exceed_SUBC-MME_{var}_{Tag}_{Region}_{FCSTDATE}_wk{W}.nc` | Pooled MME exceedance probability grid for one product/region/week |
| `manifest.json` | Run metadata: models used, variables, weeks, regions, timestamp |

### Image Files

Under `forecast/{FCSTDATE}/images/`, all PNG at 150 dpi:

| Product | Filename pattern |
|---|---|
| Anomaly panel maps | `{Prefix}{Region}Week{1-4}.png`, `{Prefix}{Region}Weeks34.png` |
| Exceedance probability maps | `{Region}/exceed_mme_{var}_{Tag}_{Region}_{FCSTDATE}_wk{W}.png` |

**Placeholder legend:**
- `{FCSTDATE}` — initialization Thursday, `YYYYMMDD`
- `{Prefix}` — variable label: `2mTemp`, `Precip`, `500hPaGeopotentialHeight`, `SfcTemp`, `LongwaveAtToa`, `MSLP`
- `{Region}` — `Global`, `NorthAmerica`, `Venezuela`, `Iran`, `Mexico`, `CAsia`
- `{var}` — short variable code: `pr`, `tas`, `zg`, etc.
- `{Tag}` — exceedance product tag, `p{percentile}_{direction}`: `p95_above` (precip/temp exceedance), `p5_below` (precip drought)
- `{W}` — week number, 1–4

---

## Reference Datasets

These are precomputed, hindcast-derived datasets reused across every weekly forecast run rather than recalculated each time.

### Climatology

**Filename:** `{var}_{group}-{model}_{MMDD}.climo.p.nc`
**Location:** `{hc_root}/{var}{lev}/daily/climo/{group}-{model}/`
**Contents:** Per-model daily climatological mean fields for each calendar day (dimensions: lead day, lat, lon).

**What it is:** Each model's own multi-year hindcast average for a given calendar day/lead. This is a **per-model** baseline, not a shared observational climatology.

**How it is used:** Each model's raw forecast is compared against its own climatology to compute an anomaly, so models with different systematic biases are placed on a comparable footing before being averaged into the SUBC-MME anomaly product.

**Why it matters:** Two models can predict very different absolute rainfall totals but agree closely once expressed as anomalies relative to their own norms.

### Percentile Thresholds

**Filename:** `{var}_{group}-{model}_{MMDD}.{pct}p.nc`
**Location:** `{hc_root}/{var}{lev}/daily/percentiles/{model}-{group}/{var}_{model}-{group}/`
**Contents:** Per-model, per-calendar-day percentile threshold fields. Both the 95th percentile (precip/temp exceedance) and 5th percentile (precip drought) are generated, per the `exceedance:` list in `config.yaml`.

**What it is:** For each model and calendar day, the value at the configured percentile (e.g. exceeded only 5% of the time, for a 95th-percentile file) in that model's own hindcast record, generated by [static/make_subx_percentiles.py](../static/make_subx_percentiles.py), which pools ensemble members from a window of days (default ±15 days) around each calendar day to build a large-enough sample for stable percentile estimates.

**How it is used:** The exceedance product compares each forecast ensemble member against this per-model threshold; models are then pooled together (summed exceedance counts / summed ensemble sizes) into the single MME exceedance map.

**Why it matters:** Precomputing model-specific percentiles once, rather than per-run, keeps weekly forecast generation fast and lets each model's own extreme-event definition be used rather than an ill-fitting shared threshold.

### Models

| Model | Center | Variables |
|---|---|---|
| ECCC-GEPS8 | Environment and Climate Change Canada | pr, rlut, tas, ts, ua, va, zg |
| EMC-GEFSv12_CPC | NOAA/NCEP EMC | pr, tas, zg |
| ESRL-FIMr1p1 | NOAA ESRL | pr, rlut, tas, ts, ua, va, zg |
| GMAO-GEOS_V2p1_5daily | NASA GMAO | pr, rlut, tas, ts, ua, va, zg |
| NCEP-CFSv2 | NOAA/NCEP | tas, pr, zg |
| RSMAS-CCSM4 | Univ. of Miami RSMAS/COLA | pr, rlut, tas, ts, ua, va, zg |
