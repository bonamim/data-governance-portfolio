"""Drive the local Airflow from a proof: trigger, wait, inspect (through the scheduler's CLI)."""

from __future__ import annotations

import json
import re
import subprocess
import time

SCHED = "governance-platform-airflow-scheduler-1"


def up() -> bool:
    return subprocess.run(["docker", "inspect", SCHED], capture_output=True).returncode == 0


def cli(*args: str) -> str:
    return subprocess.run(
        ["docker", "exec", SCHED, "airflow", *args], capture_output=True, text=True
    ).stdout


def as_json(out: str) -> list | dict:
    """The CLI's JSON, skipping any log lines printed before it."""
    m = re.search(r"^\[(\{|\])", out, re.M)
    # strict=False: error payloads carry raw newlines (stack traces) inside strings.
    return json.loads(out[m.start() :], strict=False) if m else []


def unpause(*dags: str) -> None:
    for d in dags:
        cli("dags", "unpause", d)


def dag_ids() -> list[str]:
    return sorted(d["dag_id"] for d in as_json(cli("dags", "list", "-o", "json")))


def runs(dag: str) -> list[dict]:
    return as_json(cli("dags", "list-runs", dag, "-o", "json"))


def trigger(dag: str, conf: dict | None = None) -> str:
    out = cli("dags", "trigger", dag, "--conf", json.dumps(conf or {}), "-o", "json")
    return as_json(out)[0]["dag_run_id"]


def wait(dag: str, run_id: str, timeout: int = 600) -> str:
    """The run's final state ('timeout' if it is still going)."""
    for _ in range(timeout // 5):
        state = next((r["state"] for r in runs(dag) if r["run_id"] == run_id), "")
        if state in ("success", "failed"):
            return state
        time.sleep(5)
    return "timeout"


def tasks(dag: str, run_id: str) -> dict[str, str]:
    out = cli("tasks", "states-for-dag-run", dag, run_id, "-o", "json")
    return {t["task_id"]: t["state"] for t in as_json(out)}


def import_errors() -> list[str]:
    """Files that fail to import, as ``<bundle>/<file>``."""
    errors = as_json(cli("dags", "list-import-errors", "-o", "json"))
    return [f"{e['bundle_name']}/{e['filepath']}" for e in errors]


def settle(dags: list[str], timeout: int = 900) -> None:
    """Wait until none of ``dags`` has a queued or running run."""
    for _ in range(timeout // 5):
        busy = [d for d in dags if any(r["state"] in ("queued", "running") for r in runs(d))]
        if not busy:
            return
        time.sleep(5)
    raise TimeoutError(f"still running: {busy}")
