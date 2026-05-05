#!/usr/bin/env python3
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import argparse
import yaml


ROOT_DIR = Path(__file__).resolve().parent.parent

def latest_thursday_utc():
    now = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return (now - timedelta(days=(now.weekday()-3)%7)).strftime("%Y%m%d")

def first_of_month_utc(dt=None):
    dt = dt or datetime.utcnow()
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0).strftime("%Y%m%d")

def load_cfg(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

def run_cmd(cmd_list, log_path, extra_env=None):
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "w") as log:
        env = os.environ.copy()
        if extra_env:
            env.update({k: str(v) for k, v in extra_env.items() if v is not None})
        proc = subprocess.Popen(cmd_list, stdout=log, stderr=subprocess.STDOUT, text=True, env=env)
        code = proc.wait()
        if code != 0:
            raise RuntimeError(f"Command failed ({code}): {' '.join(cmd_list)}; see {log_path}")
    return log_path

def main():
    ap = argparse.ArgumentParser(description="Unified forecast workflow runner (SubX/NMME)")
    ap.add_argument("--config", required=True, help="YAML config file")
    ap.add_argument("--init", default=None, help="Init date: SubX=YYYYMMDD, NMME=YYYYMM or YYYYMMDD")
    ap.add_argument("--system", required=True, help="subx|nmme")
    ap.add_argument("--stages", nargs="*", default=["ingest","arraylake","preprocess","products","pycpt","publish"],
                    help="Stages to run in order: ingest arraylake preprocess products pycpt publish")
    ap.add_argument("--publish-subdir", default=None,
                    help="SubX only: optional destination subdir under date, e.g., test")
    ap.add_argument("--products-dry-run", action="store_true",
                    help="SubX only: run products stage in smoke/dry-run mode")
    ap.add_argument("--products-debug", action="store_true",
                    help="SubX only: run products stage with verbose debug diagnostics")
    ap.add_argument("--pycpt-only", nargs="+", default=None,
                    help="SubX only: run PyCPT for only these region names")
    ap.add_argument("--pycpt-max-workers", type=int, default=1,
                    help="SubX only: max parallel regional PyCPT workers")
    ap.add_argument("--pycpt-models", nargs="+", default=None,
                    help="SubX only: override PyCPT model group (e.g., EMC-GEFSv12_CPC NCEP-CFSv2)")
    ap.add_argument("--pycpt-dry-run", action="store_true",
                    help="SubX only: run PyCPT in smoke mode (data prep only)")
    args = ap.parse_args()

    config_path = os.path.abspath(args.config)
    cfg = load_cfg(config_path)
    system = (args.system or cfg.get("system") or "subx").lower()
    init = args.init or (cfg.get("time", {}).get("init_date") if isinstance(cfg.get("time"), dict) else None)
    if not init:
        init = latest_thursday_utc() if system == "subx" else first_of_month_utc()

    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    logdir = os.path.join("logs", ts, system, init)
    os.makedirs(logdir, exist_ok=True)

    if system == "subx":
        stage_cmds = {
            "ingest":     [str(ROOT_DIR / "ingest" / "update_subx_fcsts.sh"), init, config_path],
            "preprocess": [str(ROOT_DIR / "preprocess" / "run_preprocess.sh"), init, config_path],
            "products":   [str(ROOT_DIR / "products" / "make_fcsts.sh"), init, config_path],
            "pycpt":      [str(ROOT_DIR / "postprocess" / "pycpt_run.sh"), init, config_path],
            "arraylake":  [str(ROOT_DIR / "arraylake" / "run_addvars_rt.sh"), init, config_path],
            "publish":    [str(ROOT_DIR / "publish" / "publish_subx_web.sh"), init, config_path],
        }
        stage_envs = {
            "ingest": {},
            "preprocess": {},
            "products": {
                "SUBX_PRODUCTS_SMOKE": "1" if args.products_dry_run else None,
                "SUBX_PRODUCTS_DEBUG": "1" if args.products_debug else None,
            },
            "pycpt": {
                "PYCPT_ONLY": ",".join(args.pycpt_only) if args.pycpt_only else None,
                "PYCPT_MAX_WORKERS": args.pycpt_max_workers,
                "PYCPT_MODELS": ",".join(args.pycpt_models) if args.pycpt_models else None,
                "PYCPT_SMOKE_MODE": "1" if args.pycpt_dry_run else None,
            },
            "publish": {
                "SUBX_PUBLISH_SUBDIR": args.publish_subdir,
            },
            "arraylake": {},
        }
    elif system == "nmme":
        # products expects YYYYMM; normalize if init like YYYYMMDD
        norm_init = init[:6]
        stage_cmds = {
            "ingest":   ["./nmme_update_fcsts.sh"],
            "products": ["./makefcsts.sh",   norm_init],
            "pycpt":    ["python3", "-c",
                           "import yaml,sys,subprocess;cfg=yaml.safe_load(open(sys.argv[1])) or {};"
                           "d=sys.argv[2];"
                           "regs=cfg.get('pycpt_regions',[]);"
                           "import shlex;"
                           "for r in regs:"
                           "  lat=r['lat']; lon=r['lon'];"
                           "  cmd=['./pycpt-seasonal_rt.py','--regname',r['name'],"
                           "       '--lat_minmax',str(lat[0]),str(lat[1]),"
                           "       '--lon_minmax',str(lon[0]),str(lon[1]),"
                           "       '--training_season',r['season'],"
                           "       '--fcstdate',d];"
                           "  subprocess.check_call(cmd)",
                           args.config, norm_init],
        }
        stage_envs = {"ingest": {}, "products": {}, "pycpt": {}}
    else:
        raise ValueError(f"Unknown system: {system}")

    for i, stage in enumerate(args.stages, 1):
        if stage not in stage_cmds:
            raise ValueError(f"Unknown stage '{stage}'. Valid: {list(stage_cmds.keys())}")
        run_cmd(
            stage_cmds[stage],
            os.path.join(logdir, f"{i:02d}_{stage}.log"),
            extra_env=stage_envs.get(stage, {}),
        )

    print(json.dumps({"system": system, "init": init, "stages": args.stages, "logdir": logdir}))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
