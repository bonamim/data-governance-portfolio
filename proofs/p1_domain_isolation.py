#!/usr/bin/env python3
"""Proof P1 — domain isolation: a domain breaks and fails alone (ADR-0003, ADR-0008).

Customer Experience is broken twice, temporarily; the other domains keep working:

  0. boundaries  no module imports across domains or from platform into a domain (CI policy)
  1. code        its DAG files import a dependency that does not exist: only its DAG bundle
                 reports import errors; Customer's and Sales' DAGs still parse, run and publish
  2. model       its output-port model gets invalid SQL: its transform fails and its port keeps
                 the last good version; Sales — which consumes that port — still builds on it
  3. recover     the files are restored; its DAGs parse and its transform succeeds again

Needs the platform and Airflow up (``make airflow-up``). Run: ``make prove-p1``.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import _airflow as af
from _checks import Checks, warehouse
from dgp_governance.repo import root

REPO = root()
BROKEN = "customer_experience"
PIPELINES = REPO / "domains" / BROKEN / "pipelines"
DAG_FILES = [PIPELINES / f"{BROKEN}_ingestion.py", PIPELINES / f"{BROKEN}_transform.py"]
MODEL = REPO / "domains" / BROKEN / "dbt" / "models" / "silver" / "reviews.sql"
PORT = "customer_experience_silver.reviews"

check = Checks("P1")


def port_rows() -> int:
    with warehouse("customer_experience_role", "CUSTOMER_EXPERIENCE_ROLE_PASSWORD") as c:
        return c.execute(f"SELECT count(*) FROM {PORT}").fetchone()[0]


def wait_errors(want_broken: bool, timeout: int = 420) -> list[str]:
    """Import errors once the DAG processor has re-parsed the bundle (state wanted reached)."""
    errors: list[str] = []
    for _ in range(timeout // 10):
        errors = af.import_errors()
        if any(e.startswith(f"{BROKEN}/") for e in errors) == want_broken:
            return errors
        time.sleep(10)
    return errors


def run_ok(dag: str) -> bool:
    run = af.trigger(dag)
    state = af.wait(dag, run)
    check(state == "success", f"{dag} runs and succeeds", state)
    return state == "success"


def step_boundaries() -> None:
    print("0. Boundaries: no cross-domain imports (CI policy)")
    r = subprocess.run(
        [sys.executable, str(REPO / "governance/policies/check_boundaries.py")],
        capture_output=True,
        text=True,
    )
    check(r.returncode == 0, "module boundaries hold", r.stdout.strip().splitlines()[-1])


def step_broken_code(originals: dict[Path, str]) -> None:
    print(f"1. Code: {BROKEN}'s DAG files import a missing dependency")
    for f in DAG_FILES:
        f.write_text(
            originals[f] + "\nimport dgp_p1_missing_dependency  # noqa: F401 (P1 probe)\n",
            encoding="utf-8",
        )
    errors = wait_errors(True)
    mine = [e for e in errors if e.startswith(f"{BROKEN}/")]
    check(len(mine) == len(DAG_FILES), f"{BROKEN}'s bundle reports import errors", ", ".join(mine))
    others = [e for e in errors if not e.startswith(f"{BROKEN}/")]
    check(not others, "no other domain has an import error", ", ".join(others) or "none")
    dags = af.dag_ids()
    alive = {"customer_ingestion", "customer_transform", "sales_ingestion", "sales_transform"}
    check(alive <= set(dags), "Customer's and Sales' DAGs are still loaded")
    run_ok("customer_transform")
    run_ok("sales_transform")


def step_broken_model(original: str) -> None:
    print(f"2. Model: {BROKEN}'s output-port model gets invalid SQL")
    before = port_rows()
    MODEL.write_text(original + "\nthis is not sql -- P1 probe\n", encoding="utf-8")
    run = af.trigger(f"{BROKEN}_transform")
    state = af.wait(f"{BROKEN}_transform", run)
    check(state == "failed", f"{BROKEN}_transform fails", state)
    check(
        af.tasks(f"{BROKEN}_transform", run).get("save_state") != "success",
        "it emits no published asset (consumers are not triggered)",
    )
    check(port_rows() == before, "its port keeps the last good version", f"{before} rows")
    run_ok("sales_transform")  # consumes customer_experience.reviews: builds on the last good one
    run_ok("customer_transform")


def step_recover(originals: dict[Path, str], model: str) -> None:
    print("3. Recover: restored, it parses and runs again")
    for f, text in originals.items():
        f.write_text(text, encoding="utf-8")
    MODEL.write_text(model, encoding="utf-8")
    errors = wait_errors(False)
    check(not [e for e in errors if e.startswith(f"{BROKEN}/")], f"{BROKEN}'s DAGs parse again")
    run_ok(f"{BROKEN}_transform")


def main() -> int:
    if not af.up():
        sys.exit("Airflow is not running — `make airflow-up` first")
    if af.import_errors():
        sys.exit(f"Airflow already has import errors: {af.import_errors()}")
    af.unpause("customer_transform", "sales_transform", f"{BROKEN}_transform")
    af.settle(af.dag_ids())
    originals = {f: f.read_text(encoding="utf-8") for f in DAG_FILES}
    model = MODEL.read_text(encoding="utf-8")
    try:
        step_boundaries()
        step_broken_code(originals)
        for f, text in originals.items():  # the code is fixed before the model is broken
            f.write_text(text, encoding="utf-8")
        wait_errors(False)
        step_broken_model(model)
        step_recover(originals, model)
    finally:
        for f, text in originals.items():
            f.write_text(text, encoding="utf-8")
        MODEL.write_text(model, encoding="utf-8")
    return check.verdict(
        "a domain's broken code or model fails only that domain — the others parse, run "
        "and publish, and its consumers keep reading its last good port."
    )


if __name__ == "__main__":
    sys.exit(main())
