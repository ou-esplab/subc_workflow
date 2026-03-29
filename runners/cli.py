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

def run_cmd(cmd_list, log_path):
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "w") as log:
        proc = subprocess.Popen(cmd_list, stdout=log, stderr=subprocess.STDOUT, text=True)
        code = proc.wait()
        if code != 0:
            raise RuntimeError(f"Command failed ({code}): {' '.join(cmd_list)}; see {log_path}")
    return log_path

def main():
    ap = argparse.ArgumentParser(description="Unified forecast workflow runner (SubX/NMME)")
    ap.add_argument("--config", required=True, help="YAML config file")
    ap.add_argument("--init", default=None, help="Init date: SubX=YYYYMMDD, NMME=YYYYMM or YYYYMMDD")
    ap.add_argument("--system", required=True, help="subx|nmme")
    ap.add_argument("--stages", nargs="*", default=["ingest","products","pycpt"],
                    help="Stages to run in order: ingest products pycpt")
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
            "ingest":   [str(ROOT_DIR / "update_subx_fcsts.sh"), init, config_path],
            "products": [str(ROOT_DIR / "make_fcsts.sh"), init, config_path],
            "pycpt":    [str(ROOT_DIR / "pycpt_run.sh"), init, config_path],
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
    else:
        raise ValueError(f"Unknown system: {system}")

    for i, stage in enumerate(args.stages, 1):
        if stage not in stage_cmds:
            raise ValueError(f"Unknown stage '{stage}'. Valid: {list(stage_cmds.keys())}")
        run_cmd(stage_cmds[stage], os.path.join(logdir, f"{i:02d}_{stage}.log"))

    print(json.dumps({"system": system, "init": init, "stages": args.stages, "logdir": logdir}))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
