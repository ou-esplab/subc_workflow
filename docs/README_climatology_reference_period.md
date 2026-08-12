# SubX climatology reference period

## Purpose

Each of the 7 configured SubX models used to have its climatology (daily mean)
and percentile threshold files built from a different, uncontrolled set of
years — whatever happened to be sitting in that model's hindcast archive
whenever its climatology script was last run. The 5th/95th percentile
exceedance thresholds were worse still: they weren't even computed from our
own hindcast archive at all, but repackaged from external pre-computed "GWSC"
files on an undocumented, uncontrolled year range.

This was discovered while investigating GEOS_V3's hindcast archive, which had
just been extended from 1999–2012 to 1999–2024, yet its climatology was still
built on the old, much shorter 1999–2012 window. Comparing models against
different climatological baselines is not scientifically sound for a
multi-model ensemble, so all 7 models' climatology and percentile thresholds
are now regenerated on one common, explicit reference period.

## Reference period: 2001-01-01 to 2016-12-27 (16 years)

This is the overlap across all 7 models' actual hindcast archives — the
longest period every model has real data for.

| Model | Hindcast archive span | Constrains the common period? |
|---|---|---|
| ECCC-GEPS8 | 2001–2020 | **Early bound** (archive starts 2001) |
| EMC-GEFSv12_CPC | 1989–2019 | No |
| ESRL-FIMr1p1 | 1999–2017 | No |
| GMAO-GEOS_V2p1_5daily | 1999–2016-12-27 | **Late bound** (archive ends here) |
| GMAO-GEOS_V3 | 1999–2024 | No (pr/tas only — see below) |
| NCEP-CFSv2 | 1999–2017 | No |
| RSMAS-CCSM4 | 1999–2016-12-31 | No (GEOS_V2p1_5daily is the tighter late bound) |

## What was (re)generated

**Climatology (daily mean)**: the full existing variable/level matrix per
model — unchanged from before, just now built on the explicit 2001–2016
window instead of auto-detected years. 36 `(group, model, var, lev)` combos
total:

| Model | Variables |
|---|---|
| ECCC-GEPS8 | pr/sfc, rlut/sfc, tas/2m, ts/sfc, ua/200, va/200, zg/500 |
| EMC-GEFSv12_CPC | pr/sfc, tas/2m, zg/500 |
| ESRL-FIMr1p1 | pr/sfc, rlut/sfc, tas/2m, ts/sfc, ua/200, va/200, zg/500 |
| GMAO-GEOS_V2p1_5daily | pr/sfc, rlut/sfc, tas/2m, ts/sfc, ua/200, va/200, zg/500 |
| GMAO-GEOS_V3 | pr/sfc, tas/2m **only** |
| NCEP-CFSv2 | pr/sfc, tas/2m, zg/500 |
| RSMAS-CCSM4 | pr/sfc, rlut/sfc, tas/2m, ts/sfc, ua/200, va/200, zg/500 |

GEOS_V3's 2-variable cap is a **hindcast-source limitation, not a bug**:
GMAO's retro portal only ever published `pr`/`tas` hindcasts for GEOS_V3 (see
`static/download_geos_v3_hindcast.py`) — there's no `rlut`/`ts`/`ua`/`va`/`zg`
hindcast data to build a climatology from for this model.

**Percentile thresholds**: self-computed from the same hindcast archive as
climatology (via `make_subx_percentiles.py`), not repackaged from external
GWSC files. Scoped to **`pr/sfc` and `tas/2m` only**, at four percentile
values — **5th and 95th** (the existing exceedance thresholds) and **33rd
and 67th** ("tercile" thresholds, new). 7 models × 2 vars × 4 percentiles =
56 combos. Percentiles are not computed for the other variables
(`rlut`/`ts`/`ua`/`va`/`zg`) because no current exceedance product uses them
— the previous GWSC-derived `zg500` percentile files were removed rather
than left stale (see Known Caveats).

**Tercile files are generated and documented only.** They are not wired into
`config.yaml`'s `exceedance:` block or any product in `products/forecast.py`
— a below/near/above-normal category product could consume them in the
future, but that's a separate, not-yet-done piece of work.

## File locations and naming

- Climatology: `{hc_root}/{var}{lev}/daily/climo/{group}-{model}/{var}_{group}-{model}_{MMDD}.climo.p.nc`
- Percentiles: `{hc_root}/{var}{lev}/daily/percentiles/{model}-{group}/{var}_{model}-{group}/{var}_{group}-{model}_{MMDD}.{pct}p.nc`

Note the percentile path's **directory** uses `{model}-{group}` order while
the **filename** uses `{group}-{model}` order — this inconsistency predates
this change and is intentionally left as-is (not part of this task's scope);
`products/forecast.py` and `utils/exceedance_utils.py` already expect it
exactly this way.

## The manifest: source of truth for "what period is this file built on"

Every `run_all_static.sh` run appends one JSON line per stage to
`{hc_root}/CLIMATOLOGY_MANIFEST.jsonl`, recording: stage (`climo`/
`percentile`), start/end year, percentile values and window (percentile stage
only), the exact list of `(group, model, var, lev)` entries processed, which
(if any) failed, the git commit, hostname, and UTC timestamp.

**Check this file, not file mtimes**, to determine what reference period any
existing climo/percentile file was actually built on. File mtimes are not
reliable for this — they get reset by unrelated operations (e.g. a full
archive re-download), which is exactly how GMAO-GEOS_V2p1_5daily's *original*
climatology period became unrecoverable before this manifest existed.

## How to regenerate or extend in the future

```bash
./static/run_all_static.sh --config config.yaml \
  --start-year YYYY --end-year YYYY --overwrite \
  [--percentiles "5,33,67,95"] [--window 15] [--workers N] [--parallel-combos N]
```

`--start-year`/`--end-year` are **mandatory, no default** — every invocation
must state the reference period explicitly.

Extending the common period later (e.g. once a model's hindcast archive grows
past today's constraint) requires a full `--overwrite` re-run of the whole
matrix, not an incremental update — both the triangular smoothing in
`make_subx_climo.py` and the ±window-day quantile pooling in
`make_subx_percentiles.py` mix samples across the *entire* selected year
range, so there's no way to "add" a few more years to an existing file
without recomputing it from scratch.

## Known caveats

- **`zg500` percentiles removed.** They were previously GWSC-sourced with an
  undocumented, uncontrolled reference period, and nothing in the codebase
  consumes them (`config.yaml`'s `exceedance:` block only defines `pr`/`tas`
  products). Rather than leave them stale/mixed-provenance next to the fresh
  2001–2016 `pr`/`tas` files, they were deleted. If a `zg500` exceedance
  product is ever built, its percentiles should be generated the same way as
  `pr`/`tas` (add `zg/500` to the percentile scope in `run_all_static.sh`).
- **`make_subx_climo.py`'s `--hindcast-dir` docstring claims CFSv2 tas
  `rt_root` data is "sea-ice only."** A live spot-check of a real file
  (2005-06-15) during this work showed physically plausible 2m-temperature
  values (global mean ≈281.9K, range 197.6–318.7K), not sea-ice data. This
  comment may be stale (`run_all_static.sh` doesn't pass `--hindcast-dir` for
  CFSv2 tas today, so it isn't actually in effect either way) — flagged here
  rather than silently trusted or silently "fixed," since it wasn't
  re-verified across the full 2001–2016 window.
- **Legacy, out-of-config percentile directories exist** under
  `{hc_root}/*/daily/percentiles/` for models no longer in `config.yaml`
  (`GEPS5/6/7-ECCC`, `GEM-ECCC`, `GEFS-EMC`, `NESM-NRL`) — pre-existing
  cruft, out of scope for this change, left untouched.
- **Performance**: opening thousands of individual small hindcast files
  turned out to be the dominant cost in climatology generation (not the
  actual computation). `make_subx_climo.py`'s all-NaN placeholder check was
  rewritten from `xarray`-based (~100-160ms/file) to raw `netCDF4`-based
  (~19ms/file) after measuring the difference directly, validated against
  the original logic with zero mismatches across 200 files spanning 5
  model/var combinations (including a pressure-level variable). Both the
  NaN-check and the main hindcast load now run through a worker pool
  (`--workers`, default 8) instead of one file at a time.
  `run_all_static.sh` also gained `--parallel-combos` (default 3) to run
  multiple model/var/lev combos concurrently as separate background jobs —
  however, a live test found concurrent combos did *not* clearly improve
  (and may have hurt) throughput on this shared storage backend, so the
  actual Phase 1/2 production runs used `--parallel-combos 1`. The flag is
  kept since it's correct and may help in other circumstances (e.g. a
  storage backend with more spare I/O capacity), just isn't the default
  recommendation established here.

## Verification results

*(Filled in after the Phase 1 and Phase 2 runs complete.)*
