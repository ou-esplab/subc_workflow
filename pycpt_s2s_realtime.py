#!/usr/bin/env python3
# coding: utf-8
"""
pycpt_s2s_realtime.py
---------------------
Run a PyCPT v2 S2S workflow for a single region and initialization date.

Features
- Downloads predictor (GEFSv12.PRCP) and predictand (CHIRPS.PRCP) via CPT-DL
- Runs CPT (CCA) using pycpt.subseasonal utilities (retroactive validation)
- Saves RPSS maps for Weeks 1–4 and Before/After downscaling plots
- Writes a pycpt_manifest.json listing outputs
- Sends email on error if email.enabled is true in config.yaml

Inputs (CLI)
--regname            Region name label (used in filenames)
--lat_minmax         Two floats: south north
--lon_minmax         Two floats: east west (CPT uses −180..180 convention)
--training_season    e.g., 'Feb-Apr'
--fcstdate           Initialization date, YYYYMMDD
--config             Path to config.yaml (default: config.yaml)

Outputs
- case_dir = paths.pycpt_case_root / <fcstdate>/
- plots_dir = paths.pycpt_case_root / 'plots/'
- RPSS PNGs, Before/After PNGs, pycpt_manifest.json
"""

import argparse
import glob
import json
import shutil
from pathlib import Path
from datetime import datetime

import yaml

from utils_email import send_email


def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def expected_local_files(predictor_names, predictand_name, leads, fcst_dt):
    expected = []
    for lead_name, lead_low, lead_high in leads:
        _ = lead_name
        for model in predictor_names:
            expected.append(f"{model}-{lead_low}-{lead_high}.nc")
            expected.append(f"{model}-{fcst_dt.year}-{fcst_dt.month}-{fcst_dt.day}-L{lead_low}-{lead_high}.nc")
        expected.append(f"{predictand_name}-{lead_low}-{lead_high}.nc")
    return expected


def stage_local_data(local_data_dir: Path, target_data_dir: Path):
    target_data_dir.mkdir(parents=True, exist_ok=True)
    for src in local_data_dir.iterdir():
        if not src.is_file():
            continue
        # Keep all downloaded artifacts available, including .nc and odd .tsv variants.
        if not (
            src.name.endswith(".nc")
            or ".tsv" in src.name
        ):
            continue
        dst = target_data_dir / src.name
        if dst.exists():
            continue
        try:
            dst.symlink_to(src.resolve())
        except OSError:
            shutil.copy2(src, dst)


def summarize_dataarray(da):
    import numpy as np

    arr = da
    vals = arr.values
    finite = np.isfinite(vals)
    finite_count = int(finite.sum())
    total_count = int(vals.size)
    summary = {
        'name': str(arr.name),
        'dims': list(arr.dims),
        'sizes': {k: int(v) for k, v in arr.sizes.items()},
        'coords': [str(c) for c in arr.coords],
        'finite_fraction': float(finite_count / total_count) if total_count else 0.0,
        'min': None,
        'max': None,
        'mean': None,
        'std': None,
    }
    if finite_count:
        fvals = vals[finite]
        summary.update(
            {
                'min': float(fvals.min()),
                'max': float(fvals.max()),
                'mean': float(fvals.mean()),
                'std': float(fvals.std()),
            }
        )
    return summary


def write_debug_summary(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=2)


def _to_0360(lon):
    return lon if lon >= 0 else lon + 360.0


def _resolve_bounds(xr_da, lon_west, lon_east, lat_south, lat_north):
    import numpy as np

    xname = 'X' if 'X' in xr_da.coords else 'lon'
    yname = 'Y' if 'Y' in xr_da.coords else 'lat'

    xvals = xr_da[xname].values
    yvals = xr_da[yname].values
    if xvals[0] > xvals[-1]:
        xr_da = xr_da.sortby(xname)
    if yvals[0] > yvals[-1]:
        xr_da = xr_da.sortby(yname)

    lon0 = _to_0360(lon_west)
    lon1 = _to_0360(lon_east)
    if lon0 <= lon1:
        xr_da = xr_da.sel({xname: slice(lon0, lon1)})
    else:
        left = xr_da.sel({xname: slice(lon0, 360)})
        right = xr_da.sel({xname: slice(0, lon1)})
        xr_da = __import__('xarray').concat([left, right], dim=xname)

    xr_da = xr_da.sel({yname: slice(lat_south, lat_north)})

    # Keep names expected downstream.
    if xname != 'X':
        xr_da = xr_da.rename({xname: 'X'})
    if yname != 'Y':
        xr_da = xr_da.rename({yname: 'Y'})
    return xr_da


def _lead_slice(da, lead_low, lead_high):
    import pandas as pd

    low = pd.to_timedelta(f"{lead_low}.5D")
    high = pd.to_timedelta(f"{lead_high}.5D")
    return da.sel(L=slice(low, high))


def _to_weekly_tp(da, lead_low, lead_high):
    w = _lead_slice(da, lead_low, lead_high)
    if 'M' in w.dims:
        w = w.mean('M')
    # SubX PR is kg m-2 s-1; weekly precipitation amount in mm.
    w = (w * 86400.0).sum('L')
    return w


def build_local_subx_predictor(
    subx_root: Path,
    model_id_local: str,
    var_local: str,
    leads,
    fdate,
    lon_west,
    lon_east,
    lat_south,
    lat_north,
    predictor_name,
    target_data_dir: Path,
):
    import xarray as xr

    fcst_dir = subx_root / model_id_local / 'forecast' / var_local
    hcst_dir = subx_root / model_id_local / 'hindcast' / var_local
    if not fcst_dir.is_dir() or not hcst_dir.is_dir():
        raise FileNotFoundError(
            f"Expected local SubX directories not found: {fcst_dir} and/or {hcst_dir}"
        )

    target_data_dir.mkdir(parents=True, exist_ok=True)

    # Build hindcast weekly files: <predictor>-<leadlow>-<leadhigh>.nc
    hc_files = sorted(glob.glob(str(hcst_dir / f"{var_local}_{model_id_local}_*.daily.nc")))
    hc_files = [f for f in hc_files if Path(f).stat().st_size > 0]
    if not hc_files:
        raise RuntimeError(f"No non-empty hindcast files found in {hcst_dir}")

    for _, lead_low, lead_high in leads:
        out = target_data_dir / f"{predictor_name}-{lead_low}-{lead_high}.nc"
        if out.exists():
            continue

        weekly_fields = []
        for f in hc_files:
            ds = xr.open_dataset(f)
            if var_local not in ds:
                ds.close()
                continue
            da = ds[var_local].isel(S=0)
            da = _resolve_bounds(da, lon_west, lon_east, lat_south, lat_north)
            wk = _to_weekly_tp(da, lead_low, lead_high)
            t = ds['S'].values[0]
            wk = wk.expand_dims(T=[t])
            wk = wk.assign_coords(Ti=('T', [t]), Tf=('T', [t]))
            weekly_fields.append(wk)
            ds.close()

        if not weekly_fields:
            raise RuntimeError(f"No usable hindcast slices for lead {lead_low}-{lead_high}")

        hc = xr.concat(weekly_fields, dim='T').sortby('T').rename('tp').to_dataset()
        hc.to_netcdf(out)

    # Build forecast weekly files: <predictor>-YYYY-M-D-L<leadlow>-<leadhigh>.nc
    fc_file = fcst_dir / f"{var_local}_{model_id_local}_{fdate.strftime('%Y%m%d')}.daily.nc"
    if not fc_file.is_file() or fc_file.stat().st_size == 0:
        raise FileNotFoundError(f"Missing non-empty forecast file for {fdate:%Y%m%d}: {fc_file}")

    dsf = xr.open_dataset(fc_file)
    if var_local not in dsf:
        dsf.close()
        raise KeyError(f"Variable '{var_local}' not found in {fc_file}")

    base = dsf[var_local].isel(S=0)
    base = _resolve_bounds(base, lon_west, lon_east, lat_south, lat_north)
    t = dsf['S'].values[0]

    for _, lead_low, lead_high in leads:
        out = target_data_dir / f"{predictor_name}-{fdate.year}-{fdate.month}-{fdate.day}-L{lead_low}-{lead_high}.nc"
        if out.exists():
            continue
        wk = _to_weekly_tp(base, lead_low, lead_high)
        wk = wk.expand_dims(T=[t]).assign_coords(Ti=('T', [t]), Tf=('T', [t]))
        wk.rename('tp').to_dataset().to_netcdf(out)

    dsf.close()


def plot_rpss_map(da, title: str, outname: str, vmin=-1, vmax=1):
    import matplotlib.pyplot as plt
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature
    import cmaps

    fig, ax = plt.subplots(figsize=(6, 6), subplot_kw={'projection': ccrs.PlateCarree()})
    cmap = cmaps.BlueWhiteOrangeRed
    da.plot(
        ax=ax,
        transform=ccrs.PlateCarree(),
        vmin=vmin, vmax=vmax,
        cmap=cmap,
        levels=11,
        cbar_kwargs={'label': 'RPSS', 'shrink': 0.7, 'orientation': 'horizontal'}
    )
    ax.coastlines(resolution='10m', linewidth=1)
    ax.add_feature(cfeature.BORDERS, linewidth=0.8)
    ax.add_feature(cfeature.STATES, linewidth=0.6)
    ax.set_title(title, fontsize=12, fontweight='bold')
    plt.tight_layout()
    plt.savefig(outname, dpi=320, bbox_inches='tight')
    plt.close(fig)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--regname', required=True)
    p.add_argument('--lat_minmax', nargs=2, type=float, required=True)
    p.add_argument('--lon_minmax', nargs=2, type=float, required=True)
    p.add_argument('--training_season', required=True)
    p.add_argument('--fcstdate', required=True)
    p.add_argument('--config', default='config.yaml')
    p.add_argument('--smoke', action='store_true', help='Smoke mode: validate data prep only, skip CPT evaluation')
    args = p.parse_args()

    # Load config & resolve paths
    cfg = yaml.safe_load(open(args.config))
    email_cfg = cfg.get('email', {})
    pycpt_cfg = cfg.get('pycpt', {}) or {}

    case_dir  = Path(cfg['paths']['pycpt_case_root']) / args.fcstdate
    plots_dir = Path(cfg['paths']['pycpt_case_root']) / 'plots'
    case_dir.mkdir(parents=True, exist_ok=True)
    plots_dir.mkdir(parents=True, exist_ok=True)

    try:
        import matplotlib.pyplot as plt
        import matplotlib.colors as mcolors
        import cartopy.crs as ccrs
        import cartopy.feature as cfeature
        import pycpt
        from pycpt import subseasonal

        MOS = 'CCA'
        predictor_names = ["GEFSv12.PRCP"]   # can be extended in config later
        predictand_name = 'CHIRPS.PRCP'

        fdate = datetime.strptime(args.fcstdate, "%Y%m%d")
        download_target = pycpt_cfg.get('target') or fdate.strftime('%b')
        use_local_data = _as_bool(pycpt_cfg.get('use_local_data'), default=False)
        use_local_subx_predictor = _as_bool(pycpt_cfg.get('use_local_subx_predictor'), default=False)
        local_data_strict = _as_bool(pycpt_cfg.get('local_data_strict'), default=False)
        force_download = _as_bool(pycpt_cfg.get('force_download'), default=not use_local_data)

        download_args = {
            'fdate':  fdate,
            'leads':  [('Week 1', 1, 7), ('Week 2', 8, 14), ('Week 3', 15, 21), ('Week 4', 22, 28)],
            # cptdl expects this key and validates downloaded season length against it.
            # For subseasonal weekly download, default to the forecast month unless
            # explicitly overridden in config.pycpt.target.
            'target': download_target,
            # pycpt's SubX URL templates still interpolate {training_season}.
            'training_season': args.training_season,
            'predictor_extent': {
                'east': args.lon_minmax[0], 'west': args.lon_minmax[1],
                'north': args.lat_minmax[1], 'south': args.lat_minmax[0]
            },
            'predictand_extent': {
                'east': args.lon_minmax[0], 'west': args.lon_minmax[1],
                'north': args.lat_minmax[1], 'south': args.lat_minmax[0]
            },
            'filetype': 'cptv10.tsv'
        }

        # Setup local domain workspace
        domain_dir = pycpt.setup(case_dir, download_args['predictor_extent'])

        if use_local_subx_predictor:
            subx_root = Path(pycpt_cfg.get('local_subx_root') or cfg['paths']['rt_root']).expanduser().resolve()
            predictor_model_id = pycpt_cfg.get('local_subx_model_id', 'EMC-GEFSv12_CPC')
            var_map = {'PRCP': 'pr', 'T2M': 'tas', 'TMP': 'tas'}
            for predictor_name in predictor_names:
                model_tag, var_tag = predictor_name.split('.', 1)
                var_local = var_map.get(var_tag.upper(), var_tag.lower())
                _ = model_tag
                build_local_subx_predictor(
                    subx_root=subx_root,
                    model_id_local=predictor_model_id,
                    var_local=var_local,
                    leads=download_args['leads'],
                    fdate=fdate,
                    lon_west=args.lon_minmax[1],
                    lon_east=args.lon_minmax[0],
                    lat_south=args.lat_minmax[0],
                    lat_north=args.lat_minmax[1],
                    predictor_name=predictor_name,
                    target_data_dir=domain_dir / 'data',
                )
            # Keep observations download enabled unless fully staged local files are provided.
            force_download = False

        if use_local_data:
            local_data_dir_raw = pycpt_cfg.get('local_data_dir')
            if not local_data_dir_raw:
                raise ValueError("pycpt.use_local_data=true requires pycpt.local_data_dir in config")

            local_data_dir = Path(local_data_dir_raw).expanduser().resolve()
            if not local_data_dir.is_dir():
                raise FileNotFoundError(f"Configured pycpt.local_data_dir does not exist: {local_data_dir}")

            stage_local_data(local_data_dir, domain_dir / 'data')
            expected = expected_local_files(predictor_names, predictand_name, download_args['leads'], fdate)
            missing = [name for name in expected if not (domain_dir / 'data' / name).is_file()]
            if missing and local_data_strict:
                raise RuntimeError(
                    "Missing required local PyCPT files in strict mode:\n"
                    + "\n".join(f"- {name}" for name in missing)
                )
            if missing:
                print(f"[WARN] Missing {len(missing)} local PyCPT files; downloader may fetch remaining inputs.")
            else:
                print("[INFO] All required local PyCPT files are present; no download should be needed.")
            force_download = False

        # Download hindcasts/observations/forecasts from IRIDL via CPT-DL
        hindcast_data, Y, forecast_data = subseasonal.download_data(
            predictor_names, predictand_name, download_args, domain_dir, force_download=force_download
        )

        print('[PyCPT] Predictor hindcast summary:')
        print(json.dumps(summarize_dataarray(hindcast_data), indent=2))
        print('[PyCPT] Predictand observation summary:')
        print(json.dumps(summarize_dataarray(Y), indent=2))
        print('[PyCPT] Predictor forecast summary:')
        print(json.dumps(summarize_dataarray(forecast_data), indent=2))

        # Smoke mode: exit after data validation, skip expensive CPT evaluation
        if args.smoke:
            print('[PyCPT] SMOKE MODE: Data pipeline validated, skipping CPT evaluation.')
            return

        # CPT arguments (matches your working settings)
        cpt_args = {
            'transform_predictand': None,
            'tailoring': None,
            'cca_modes': (1, 3),
            'x_eof_modes': (1, 8),
            'y_eof_modes': (1, 6),
            'validation': 'retroactive',
            'drymask': False,
            'scree': True,
            'crossvalidation_window': 5,
            'synchronous_predictors': True,
            'retroactive_initial_training_period': 45,
            'retroactive_step': 10,
            'cpt_kwargs': {}
        }

        # Evaluate models: returns hindcasts, forecasts, skill & modes
        try:
            hcsts, fcsts, skill, pxs, pys = subseasonal.evaluate_models(
                hindcast_data, forecast_data, Y, MOS, cpt_args, domain_dir
            )
        except Exception as cpt_exc:
            debug_payload = {
                'error': str(cpt_exc),
                'fcstdate': args.fcstdate,
                'region': args.regname,
                'training_season': args.training_season,
                'download_args': {
                    'target': download_args.get('target'),
                    'training_season': download_args.get('training_season'),
                    'leads': download_args.get('leads'),
                },
                'hindcast_summary': summarize_dataarray(hindcast_data),
                'observation_summary': summarize_dataarray(Y),
                'forecast_summary': summarize_dataarray(forecast_data),
                'cpt_args': cpt_args,
            }
            write_debug_summary(case_dir / 'pycpt_debug_summary.json', debug_payload)
            raise

        # RPSS maps for Weeks 1–4 (model 0)
        rpss = skill['rank_probability_skill_score'].isel(model=0)
        ic_title = download_args['fdate'].strftime("%m/%d/%Y")
        ic_file  = download_args['fdate'].strftime("%m%d%Y")
        outfiles = []

        for wk in [1, 2, 3, 4]:
            da = rpss.sel(lead_name=f'Week {wk}')
            out = plots_dir / f"pycpt_rpss_w{wk}_{args.regname}_{args.training_season.replace('/','-')}_{ic_file}.png"
            plot_rpss_map(da, f"RPSS Week {wk} (GEFS–PyCPT) | IC: {ic_title}", str(out))
            outfiles.append(str(out))

        # Before/After downscaling comparison (Week 1–4)
        vmin, vmax = -15, 15
        norm = mcolors.TwoSlopeNorm(vmin=vmin, vcenter=0, vmax=vmax)
        for lead in range(4):
            fb = forecast_data.isel(lead_name=lead, model=0, T=0)
            fa = fcsts['deterministic'].isel(lead_name=lead, model=0, T=0)
            fig, axes = plt.subplots(1, 2, figsize=(12, 5), subplot_kw={'projection': ccrs.PlateCarree()})
            for ax in axes:
                ax.coastlines('10m', linewidth=1)
                ax.add_feature(cfeature.BORDERS, linewidth=1)
                ax.add_feature(cfeature.STATES, linewidth=0.6)
            im1 = axes[0].pcolormesh(fb['X'], fb['Y'], fb, cmap='BrBG', norm=norm, transform=ccrs.PlateCarree())
            axes[0].set_title(f"Before (GEFSv12) Week {lead+1} | IC: {ic_title}")
            im2 = axes[1].pcolormesh(fa['X'], fa['Y'], fa, cmap='BrBG', norm=norm, transform=ccrs.PlateCarree())
            axes[1].set_title(f"After Downscaling Week {lead+1} | IC: {ic_title}")
            fig.colorbar(im1, ax=axes, orientation='horizontal', fraction=0.05, pad=0.1, extend='both')
            out = plots_dir / f"bc_week{lead+1}_GEFSv12_CPC_{args.regname}_{ic_file}.png"
            plt.savefig(out, dpi=300, bbox_inches='tight'); plt.close(fig)
            outfiles.append(str(out))

        # Manifest
        manifest = {
            'region': args.regname,
            'fcstdate': args.fcstdate,
            'training_season': args.training_season,
            'case_dir': str(case_dir),
            'plots_dir': str(plots_dir),
            'files': outfiles,
            'timestamp': datetime.utcnow().isoformat()+"Z"
        }
        with open(case_dir / 'pycpt_manifest.json','w') as f:
            json.dump(manifest, f, indent=2)
        print('[PyCPT] complete')

    except Exception as e:
        # Email on error (best effort)
        body = f"PyCPT failed for {args.regname} {args.fcstdate}:\n{e}"
        try:
            send_email(email_cfg, f"PyCPT error {args.fcstdate} {args.regname}", body)
        except Exception:
            pass
        raise

if __name__ == '__main__':
    main()