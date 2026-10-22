"""
Shared helpers for syncing SubX raw model files to NCAR GDEX over Globus.

Thin subprocess wrappers around the `globus-cli` (`globus login` once,
then `globus ls` / `globus transfer` / `globus task wait`) rather than the
Globus Python SDK, since the one-time OAuth login and token cache
(~/.globus/cli) are managed entirely outside this repo.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import tempfile
from concurrent.futures import ThreadPoolExecutor


def load_gdex_config(config_path: str) -> dict:
    import yaml

    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    gdex_cfg = cfg.get("gdex") or {}
    models = cfg.get("models") or []
    if not models:
        raise RuntimeError("No models found in config.yaml under models")

    runtime = {
        "enabled": bool(gdex_cfg.get("enabled", False)),
        "source_endpoint_id": gdex_cfg.get("source_endpoint_id"),
        "dest_endpoint_id": gdex_cfg.get("dest_endpoint_id"),
        "dest_root": str(gdex_cfg.get("dest_root") or "").rstrip("/"),
        "input_root": gdex_cfg.get("input_root") or ((cfg.get("paths") or {}).get("rt_root")),
        "variables": gdex_cfg.get("variables") or ["pr", "tas", "rlut", "ts", "ua", "va", "zg"],
        "skip_models": set(gdex_cfg.get("skip_models") or []),
        "model_vars": gdex_cfg.get("model_vars") or {},
        "task_poll_interval_s": int(gdex_cfg.get("task_poll_interval_s", 30)),
        "task_wait_timeout_s": int(gdex_cfg.get("task_wait_timeout_s", 3600)),
        "gcp_path": os.path.expanduser(str(gdex_cfg.get("gcp_path") or "")),
        "gcp_screen_name": str(gdex_cfg.get("gcp_screen_name") or "gcp_endpoint"),
        "gcp_start_timeout_s": int(gdex_cfg.get("gcp_start_timeout_s", 120)),
        "batch_chunk_size": int(gdex_cfg.get("batch_chunk_size", 5000)),
        "max_active_tasks": int(gdex_cfg.get("max_active_tasks", 3)),
        "models": models,
    }
    if not runtime["input_root"]:
        raise RuntimeError("gdex.input_root or paths.rt_root must be set in config.yaml")
    for key in ("source_endpoint_id", "dest_endpoint_id", "dest_root"):
        value = runtime[key]
        if not value or (isinstance(value, str) and value.strip().startswith("<FILL IN")):
            raise RuntimeError(f"gdex.{key} must be set in config.yaml (got {value!r})")
    return runtime


def resolve_models(runtime: dict):
    """Yield (model_label, effective_vars) honoring gdex.skip_models/model_vars."""
    cfg_variables = list(runtime["variables"])
    skip_models = runtime["skip_models"]
    model_vars = runtime["model_vars"]
    for model in runtime["models"]:
        group = model.get("group")
        name = model.get("name")
        if not group or not name:
            continue
        label = f"{group}-{name}"
        if label in skip_models:
            continue
        raw = model_vars.get(label, model_vars.get(name))
        if raw is None:
            effective = cfg_variables
        elif isinstance(raw, str):
            effective = [raw]
        else:
            effective = list(raw)
        yield label, effective


def _globus_base_cmd() -> list[str]:
    """Prefix for invoking the `globus` CLI.

    globus-cli needs Python's sqlite3 module for its OAuth token cache,
    which is broken in the shared subc_workflow_env (unrelated, pre-existing
    conda package conflict) -- so globus-cli lives in its own dedicated
    conda env instead. Set SUBC_GDEX_ENV to route through it via `conda run`
    (mirrors SUBC_PYCPT_ENV's isolation of PyCPT); leave unset to call
    `globus` directly off PATH.
    """
    gdex_env = os.environ.get("SUBC_GDEX_ENV", "").strip()
    if gdex_env:
        return ["conda", "run", "--no-capture-output", "-n", gdex_env, "globus"]
    return ["globus"]


def globus_ls(endpoint_id: str, path: str) -> list[str]:
    """List filenames (not subdirectories) in a remote directory.

    Returns [] if the directory doesn't exist yet on the destination --
    that's the normal, expected state for a model/variable never synced
    before, not an error.
    """
    proc = subprocess.run(
        _globus_base_cmd() + ["ls", f"{endpoint_id}:{path}", "--format", "json"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or "").lower()
        if "no such file or directory" in stderr or "not found" in stderr:
            return []
        raise RuntimeError(f"globus ls failed for {endpoint_id}:{path}: {proc.stderr.strip()}")
    data = json.loads(proc.stdout or "{}")
    return [entry["name"] for entry in data.get("DATA", []) if entry.get("type") == "file"]


def _parse_task_id(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.lower().startswith("task id:"):
            return line.split(":", 1)[1].strip()
    raise RuntimeError(f"Could not parse Task ID from globus transfer output:\n{stdout}")


def globus_transfer_batch(
    src_endpoint_id: str,
    dst_endpoint_id: str,
    src_root: str,
    dst_root: str,
    relative_pairs: list[tuple[str, str]],
    label: str,
) -> str | None:
    """Submit one batch transfer covering multiple (src_rel, dst_rel) file pairs.

    Paths in relative_pairs are relative to src_root/dst_root. Returns the
    submitted task ID, or None if relative_pairs is empty (nothing to do).
    """
    if not relative_pairs:
        return None
    fd, batch_path = tempfile.mkstemp(suffix=".batch", text=True)
    try:
        with os.fdopen(fd, "w") as f:
            for src_rel, dst_rel in relative_pairs:
                f.write(f"{src_rel} {dst_rel}\n")
        cmd = _globus_base_cmd() + [
            "transfer",
            f"{src_endpoint_id}:{src_root}",
            f"{dst_endpoint_id}:{dst_root}",
            "--batch", batch_path,
            "--label", label,
            "--sync-level", "checksum",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"globus transfer --batch failed: {proc.stderr.strip()}")
        return _parse_task_id(proc.stdout)
    finally:
        os.unlink(batch_path)


def globus_transfer_recursive(
    src_endpoint_id: str,
    dst_endpoint_id: str,
    src_path: str,
    dst_path: str,
    label: str,
) -> str:
    """Submit one recursive directory transfer. Returns the submitted task ID."""
    cmd = _globus_base_cmd() + [
        "transfer",
        f"{src_endpoint_id}:{src_path}",
        f"{dst_endpoint_id}:{dst_path}",
        "--recursive",
        "--label", label,
        "--sync-level", "checksum",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"globus transfer --recursive failed: {proc.stderr.strip()}")
    return _parse_task_id(proc.stdout)


def globus_task_wait(task_id: str, poll_interval_s: int, timeout_s: int) -> tuple[bool, str]:
    """Block until a Globus task finishes. Returns (succeeded, detail)."""
    proc = subprocess.run(
        _globus_base_cmd() + [
            "task", "wait", task_id,
            "--timeout", str(timeout_s),
            "--polling-interval", str(poll_interval_s),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        return True, "SUCCEEDED"

    show = subprocess.run(
        _globus_base_cmd() + ["task", "show", task_id, "--format", "json"],
        capture_output=True,
        text=True,
    )
    detail = proc.stderr.strip() or proc.stdout.strip() or "task did not succeed"
    if show.returncode == 0:
        try:
            info = json.loads(show.stdout or "{}")
            status = info.get("status", "UNKNOWN")
            nice_error = info.get("nice_status_short_description")
            detail = f"status={status}" + (f" ({nice_error})" if nice_error else "")
        except json.JSONDecodeError:
            pass
    return False, detail


def globus_task_status(task_id: str) -> str:
    """Current status of a Globus task (ACTIVE, INACTIVE, SUCCEEDED, FAILED), or UNKNOWN if it can't be read."""
    proc = subprocess.run(
        _globus_base_cmd() + ["task", "show", task_id, "--format", "json"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return "UNKNOWN"
    try:
        return json.loads(proc.stdout or "{}").get("status", "UNKNOWN")
    except json.JSONDecodeError:
        return "UNKNOWN"


def globus_find_unfinished_task(label_prefix: str):
    """Return the ID of a not-yet-finished (ACTIVE/INACTIVE) task whose label starts with label_prefix, else None.

    Used to re-attach to an in-flight transfer instead of resubmitting it (Globus rejects a
    resubmit of identical paths with 409 while the first task is unfinished).
    """
    for status in ("ACTIVE", "INACTIVE"):
        proc = subprocess.run(
            _globus_base_cmd() + [
                "task", "list", "--filter-status", status, "--filter-type", "TRANSFER",
                "--filter-label", f"{label_prefix}*", "--inexact", "--limit", "20", "--format", "json",
            ],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            continue
        try:
            tasks = json.loads(proc.stdout or "{}").get("DATA", [])
        except json.JSONDecodeError:
            continue
        if tasks:
            return tasks[0]["task_id"]
    return None


def transfer_batches_parallel(runtime: dict, jobs: dict, label_prefix: str, logger=None) -> dict:
    """Transfer {model_label: [(src_rel, dst_rel), ...]} as chunked batch tasks, run concurrently.

    Each model's pairs are split into chunks of gdex.batch_chunk_size so one bad
    file only fails (and retries) its own chunk, and up to gdex.max_active_tasks
    chunk tasks are submitted/waited on at once (Globus allows 3 active tasks
    per user; extras would just queue). Returns {model_label: (ok, detail)}.
    """
    chunk_size = max(1, runtime["batch_chunk_size"])
    work = []  # (label, chunk_idx, n_chunks, chunk_pairs)
    for label, pairs in jobs.items():
        chunks = [pairs[i:i + chunk_size] for i in range(0, len(pairs), chunk_size)]
        for idx, chunk in enumerate(chunks, 1):
            work.append((label, idx, len(chunks), chunk))

    def run_one(item):
        label, idx, n, chunk = item
        try:
            task_id = globus_transfer_batch(
                runtime["source_endpoint_id"], runtime["dest_endpoint_id"],
                str(runtime["input_root"]).rstrip("/"), runtime["dest_root"], chunk,
                label=f"{label_prefix}-{label}-{idx}of{n}",
            )
            if logger:
                logger.info(f"{label}: chunk {idx}/{n} ({len(chunk)} file(s)) submitted as task {task_id}")
            while True:
                ok, detail = globus_task_wait(
                    task_id, runtime["task_poll_interval_s"], runtime["task_wait_timeout_s"]
                )
                # A wait timeout on a still-alive task is not a failure: keep waiting on it.
                if ok:
                    break
                status = globus_task_status(task_id)
                if status == "SUCCEEDED":  # finished between the wait timeout and this check
                    ok, detail = True, "SUCCEEDED"
                    break
                if status not in ("ACTIVE", "INACTIVE"):
                    break
                if logger:
                    logger.info(f"{label}: chunk {idx}/{n} task {task_id} still active after wait timeout; continuing to wait")
            if logger:
                logger.info(f"{label}: chunk {idx}/{n} task {task_id}: {'SUCCEEDED' if ok else detail}")
            return label, ok, (f"chunk {idx}/{n} task {task_id}: {detail}" if not ok else "")
        except Exception as e:
            return label, False, f"chunk {idx}/{n} error: {e}"

    failures = {label: [] for label in jobs}
    with ThreadPoolExecutor(max_workers=max(1, runtime["max_active_tasks"])) as pool:
        for label, ok, detail in pool.map(run_one, work):
            if not ok:
                failures[label].append(detail)

    results = {}
    for label, pairs in jobs.items():
        if failures[label]:
            results[label] = (False, f"{len(failures[label])} chunk(s) failed: " + "; ".join(failures[label]))
        else:
            results[label] = (True, f"Transferred {len(pairs)} file(s)")
    return results


def _gcp_connected(endpoint_id: str) -> bool:
    """True if Globus reports the GCP endpoint as connected (the service-side view that transfers depend on)."""
    proc = subprocess.run(
        _globus_base_cmd() + ["endpoint", "show", endpoint_id, "--format", "json"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"globus endpoint show {endpoint_id} failed: {proc.stderr.strip()}")
    return bool(json.loads(proc.stdout or "{}").get("gcp_connected"))


def ensure_source_endpoint_online(runtime: dict, logger=None) -> None:
    """Make sure the source Globus Connect Personal endpoint is connected, restarting it if not.

    The source is a personal endpoint (no managed endpoint available), which only
    serves files while its `globusconnectpersonal -start` process is alive. If it is
    offline, stop any stale process, relaunch it in a detached `screen` session, and
    poll until Globus sees it connected. Raises RuntimeError if it cannot be brought up.
    """
    log = (lambda m: logger.info(m)) if logger else (lambda m: None)
    endpoint_id = runtime["source_endpoint_id"]
    if _gcp_connected(endpoint_id):
        return
    gcp = runtime.get("gcp_path")
    if not gcp or not os.path.isfile(gcp):
        raise RuntimeError(f"source endpoint {endpoint_id} is offline and gdex.gcp_path ({gcp!r}) is not usable to restart it")
    screen_name = runtime["gcp_screen_name"]
    log(f"source endpoint {endpoint_id} is offline; restarting Globus Connect Personal in screen '{screen_name}'")
    subprocess.run([gcp, "-stop"], capture_output=True, text=True)
    subprocess.run(["screen", "-S", screen_name, "-X", "quit"], capture_output=True, text=True)  # drop stale session, if any
    proc = subprocess.run(["screen", "-dmS", screen_name, gcp, "-start"], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"could not launch screen session '{screen_name}': {proc.stderr.strip()}")
    deadline = time.time() + runtime["gcp_start_timeout_s"]
    while time.time() < deadline:
        time.sleep(5)
        if _gcp_connected(endpoint_id):
            log("source endpoint is back online")
            return
    raise RuntimeError(f"source endpoint {endpoint_id} did not come online within {runtime['gcp_start_timeout_s']}s")
