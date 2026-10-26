#!/usr/bin/env python3
"""Proof P4 — cross-domain automation: consumers react to producers, not to schedules.

``sales.sales_performance`` consumes ``customer.customers`` (dbt-loom, a data product input). The
sales transform DAG is scheduled on ANY of its inputs' assets — ``raw/sales``,
``published/customer``, ``published/customer_experience`` — never on a clock:

  1. publish   the customer domain republishes its ports (``customer_transform``, run by hand)
  2. react     ``published/customer`` alone triggers ``sales_transform`` (an asset-triggered run),
               which rebuilds sales_performance
  3. nothing   no other domain DAG runs: not the ingestions, not customer_experience's transform

Needs the platform and Airflow up (``make airflow-up``). Run: ``make prove-p4``.
"""

from __future__ import annotations

import sys
import time
from datetime import UTC, datetime

import _airflow as af
from _checks import Checks, warehouse

PRODUCER, CONSUMER = "customer_transform", "sales_transform"
# Clock-scheduled platform DAGs may run during the proof; they are not domain reactions.
CLOCKED = {"platform_landing_sla"}

check = Checks("P4")


def perf_version() -> str:
    with warehouse("sales_role", "SALES_ROLE_PASSWORD") as c:
        return c.execute("SELECT min(xmin::text) FROM sales_gold.sales_performance").fetchone()[0]


def main() -> int:
    if not af.up():
        sys.exit("Airflow is not running — `make airflow-up` first")
    dags = [d for d in af.dag_ids() if d not in CLOCKED]
    af.unpause(PRODUCER, CONSUMER, "customer_experience_transform")
    print("   waiting for in-flight runs to finish (a clean baseline)")
    af.settle(dags)
    before = {d: {r["run_id"] for r in af.runs(d)} for d in dags}
    version = perf_version()
    start = datetime.now(UTC)

    print("1. Publish: the customer domain republishes its output ports")
    run = af.trigger(PRODUCER)
    check(af.wait(PRODUCER, run) == "success", f"{PRODUCER} succeeded", run)
    check(
        af.tasks(PRODUCER, run).get("save_state") == "success",
        "it emitted published/customer (save_state)",
    )

    print("2. React: the consumer rebuilds because its input changed")
    reaction = ""
    for _ in range(60):
        new = [r for r in af.runs(CONSUMER) if r["run_id"] not in before[CONSUMER]]
        if new:
            reaction = new[0]["run_id"]
            break
        time.sleep(5)
    check(
        reaction.startswith("asset_triggered__"),
        f"{CONSUMER} was triggered by the asset",
        reaction or "no run",
    )
    if reaction:
        check(af.wait(CONSUMER, reaction) == "success", f"{CONSUMER} succeeded")
        check(perf_version() != version, "sales_performance was rebuilt")

    print("3. Nothing else: no other domain DAG ran")
    af.settle(dags)
    ran = sorted(d for d in dags if {r["run_id"] for r in af.runs(d)} - before[d])
    check(
        ran == sorted([PRODUCER, CONSUMER]),
        "only the producer and its consumer ran",
        ", ".join(ran),
    )
    print(f"   window: {start:%H:%M:%S} .. {datetime.now(UTC):%H:%M:%S} UTC")

    return check.verdict(
        "a new version of a producer's port rebuilds exactly the domains that consume it, "
        "through assets — no schedule, no manual trigger downstream, nothing else runs."
    )


if __name__ == "__main__":
    sys.exit(main())
