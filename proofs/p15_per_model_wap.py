#!/usr/bin/env python3
"""Proof P15 — per-model write-audit-publish, dependency-safe (ADR-0015 §1–§2).

Exercised on the sales domain, whose two output ports opt in to WAP: ``sales.orders`` (a monthly
microbatch table) and ``sales.sales_performance`` (a table built from it).

  1. promote    a normal publish replaces sales_performance IN PLACE: same table (OID), a
                consumer's dependent view, its grants and its docs all survive
  2. bad batch  a raw order with no customer in Jan 2017, backfilled -> the orders audit fails ->
                quarantined: the port keeps its last good rows, candidate and audit view dropped,
                sales_performance is not rebuilt from it (upstream audit failed)
  3. table rule a raw order that is clean on its own but re-uses an order id from another month:
                the batch alone has no duplicate, the table would -> the audit, which runs on the
                table as it would be after the merge, quarantines it
  4. recover    the bad rows removed, the same backfill passes and publishes
  5. airflow    (if Airflow is up) the sales_transform DAG renders one task group per model and a
                wap_<model> pipeline per opted-in port; a quarantine there fails the audit task,
                runs quarantine, and save_state — the only task emitting published/sales — does not
                run, so consuming domains are not triggered (proof P3)

Leaves the warehouse as it found it. Run: ``make prove-p15`` (``--skip-airflow`` to skip step 5).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time

import psycopg
from _checks import Checks, warehouse
from dgp_governance.repo import root

REPO = root()
PORT, PERF = "sales_silver.orders", "sales_gold.sales_performance"
INSERT = (
    "INSERT INTO sales_raw.orders (order_id, customer_id, order_status, order_purchase_timestamp)"
    " VALUES (%s, %s, 'delivered', '2017-01-15 10:00:00')"
)
CONSUMER = "sales_gold.p15_consumer"
WINDOW = ["--event-time-start", "2017-01-01", "--event-time-end", "2017-02-01"]
SCHED = "governance-platform-airflow-scheduler-1"

check = Checks("P15")


def conn(user: str, pw_env: str) -> psycopg.Connection:
    return warehouse(user, pw_env, autocommit=True)


def sales(sql: str, *args):
    with conn("sales_role", "SALES_ROLE_PASSWORD") as c:
        cur = c.execute(sql, args)
        return cur.fetchall() if cur.description else []


def raw(sql: str, *args) -> None:
    with conn("platform_loader", "PLATFORM_LOADER_PASSWORD") as c:
        c.execute(sql, args)


def publish(*extra: str) -> tuple[int, str]:
    r = subprocess.run(
        [
            sys.executable,
            str(REPO / "platform" / "core" / "transform" / "publish.py"),
            "--domain",
            "sales",
            "--no-state",
            *extra,
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    return r.returncode, r.stdout + r.stderr


def snapshot() -> dict:
    return {
        "orders": sales(f"SELECT count(*) FROM {PORT}")[0][0],
        "perf_oid": sales(f"SELECT '{PERF}'::regclass::oid")[0][0],
        "perf_rows": sales(f"SELECT count(*), sum(total_revenue)::text FROM {PERF}")[0],
        "perf_acl": sales(f"SELECT relacl::text FROM pg_class WHERE oid = '{PERF}'::regclass")[0][
            0
        ],
    }


def leftovers() -> list[str]:
    return [
        r[0]
        for r in sales(
            "SELECT n.nspname || '.' || c.relname FROM pg_class c"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname LIKE 'sales\\_%%' AND c.relname ~ '__(candidate|audit)$'"
        )
    ]


def step_promote(base: dict) -> None:
    print("1. Promote: a same-shape table is replaced in place — dependents and grants survive")
    sales(f"CREATE OR REPLACE VIEW {CONSUMER} AS SELECT * FROM {PERF}")
    rc, log = publish()
    check(rc == 0, "publish succeeded", "")
    check("replaced in place" in log, "sales_performance promoted in place (not dropped/renamed)")
    after = snapshot()
    check(
        after["perf_oid"] == base["perf_oid"], "same table identity (OID)", str(after["perf_oid"])
    )
    check(
        sales(f"SELECT to_regclass('{CONSUMER}') IS NOT NULL")[0][0],
        "the consumer's dependent view survived (no DROP ... CASCADE)",
    )
    check(after["perf_acl"] == base["perf_acl"], "grants unchanged", after["perf_acl"])
    check(after["perf_rows"] == base["perf_rows"], "same rows", f"{after['perf_rows']}")
    doc = sales(f"SELECT obj_description('{PERF}'::regclass, 'pg_class')")[0][0]
    check(bool(doc), "table docs carried over from the candidate", (doc or "")[:60])


def step_quarantine(base: dict, label: str, row: tuple, rule: str) -> None:
    raw(INSERT, *row)
    try:
        rc, log = publish(*WINDOW)
    finally:
        raw(
            "DELETE FROM sales_raw.orders WHERE order_purchase_timestamp = '2017-01-15 10:00:00'"
            " AND order_status = 'delivered' AND order_id = %s",
            row[0],
        )
    check(rc != 0, f"{label}: publish reports the quarantine (non-zero exit)")
    check(rule in log, f"{label}: the failing rule is named", rule)
    check("QUARANTINED sales_silver.orders" in log, f"{label}: orders quarantined")
    check(
        "skipped sales.sales_performance: an upstream audit failed" in log,
        f"{label}: sales_performance not rebuilt from the bad batch",
    )
    after = snapshot()
    check(
        after["orders"] == base["orders"],
        f"{label}: port keeps its last good rows",
        f"{after['orders']} rows",
    )
    check(
        not sales(
            f"SELECT 1 FROM {PORT} WHERE order_id = %s AND customer_id IS NOT DISTINCT FROM"
            " %s::text AND order_purchase_timestamp = '2017-01-15 10:00:00'",
            *row,
        ),
        f"{label}: the bad row is not in the published table",
    )
    check(after["perf_rows"] == base["perf_rows"], f"{label}: sales_performance untouched")
    check(not leftovers(), f"{label}: candidate and audit view dropped", ", ".join(leftovers()))


def step_recover(base: dict) -> None:
    print("4. Recover: the bad rows removed, the same backfill passes")
    rc, log = publish(*WINDOW)
    check(rc == 0 and "merged batches 2017-01-01" in log, "backfill audited and merged")
    check(snapshot()["orders"] == base["orders"], "row count back to the baseline")


def _airflow(*args: str) -> str:
    r = subprocess.run(["docker", "exec", SCHED, "airflow", *args], capture_output=True, text=True)
    return r.stdout


def _json(out: str):
    """Airflow CLI JSON, skipping any log lines printed before it."""
    m = re.search(r"^\[(\{|\])", out, re.M)
    return json.loads(out[m.start() :]) if m else []


def _run(conf: dict) -> tuple[str, dict[str, str]]:
    out = _airflow("dags", "trigger", "sales_transform", "--conf", json.dumps(conf), "-o", "json")
    run_id = _json(out)[0]["dag_run_id"]
    state = ""
    for _ in range(90):
        time.sleep(5)
        runs = _json(_airflow("dags", "list-runs", "sales_transform", "-o", "json"))
        state = next((r["state"] for r in runs if r["run_id"] == run_id), "")
        if state not in ("running", "queued", ""):
            break
    tasks = _json(_airflow("tasks", "states-for-dag-run", "sales_transform", run_id, "-o", "json"))
    return state, {t["task_id"]: t["state"] for t in tasks}


def step_airflow(base: dict) -> None:
    print("5. Airflow: one task group per model; a quarantine stops the published/sales event")
    tasks = _airflow("tasks", "list", "sales_transform").split()
    check(
        {
            "models.brz_orders.run",
            "models.brz_orders.test",
            "wap_orders.audit",
            "wap_orders.publish",
            "wap_sales_performance.audit",
            "save_state",
        }
        <= set(tasks),
        "Cosmos renders per-model tasks + a WAP pipeline per opted-in port",
        f"{len(tasks)} tasks",
    )
    _airflow("dags", "unpause", "sales_transform")
    raw(INSERT, "p15airflow", None)
    try:
        state, ts = _run({"event_time_start": "2017-01-01", "event_time_end": "2017-02-01"})
    finally:
        raw("DELETE FROM sales_raw.orders WHERE order_id = 'p15airflow'")
    check(
        ts.get("wap_orders.audit") == "failed",
        "wap_orders.audit failed",
        ts.get("wap_orders.audit"),
    )
    check(ts.get("wap_orders.quarantine") == "success", "wap_orders.quarantine ran")
    check(ts.get("wap_orders.publish") == "upstream_failed", "wap_orders.publish did not run")
    check(
        ts.get("wap_sales_performance.plan") == "upstream_failed",
        "sales_performance not rebuilt from it",
    )
    check(ts.get("grant") == "success", "grants re-applied anyway")
    check(
        ts.get("save_state") != "success",
        "save_state (the only published/sales outlet) did not run — consumers not triggered",
        ts.get("save_state"),
    )
    check(state == "failed", "the DAG run is failed, so the breach is visible", state)
    check(snapshot()["orders"] == base["orders"], "port unchanged after the Airflow quarantine")
    state, ts = _run({"event_time_start": "2017-01-01", "event_time_end": "2017-02-01"})
    check(
        state == "success" and ts.get("save_state") == "success",
        "the clean re-run publishes and emits published/sales",
        state,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Proof P15 — per-model write-audit-publish")
    parser.add_argument("--skip-airflow", action="store_true", help="skip the Airflow step")
    args = parser.parse_args()
    try:
        base = snapshot()
    except psycopg.OperationalError as e:
        sys.exit(f"analytics warehouse unreachable — `make up` first ({e})")
    if leftovers():
        sys.exit(f"stale WAP relations {leftovers()} — run `make publish` first")
    try:
        step_promote(base)
        print("2. Bad batch: a row breaking a column rule is quarantined, never published")
        step_quarantine(base, "null key", ("p15null", None), "not_null_orders_customer_id")
        print("3. Table rule: a batch clean on its own but duplicating the table is quarantined")
        dup = sales(
            f"SELECT order_id FROM {PORT} WHERE order_purchase_timestamp >= '2018-01-01'"
            " ORDER BY order_id LIMIT 1"
        )[0][0]
        step_quarantine(base, "cross-batch duplicate", (dup, "p15c"), "unique_orders_order_id")
        step_recover(base)
        if not args.skip_airflow:
            if subprocess.run(["docker", "inspect", SCHED], capture_output=True).returncode == 0:
                step_airflow(base)
            else:
                print("5. Airflow: not running — skipped (`make airflow-up`)")
    finally:
        sales(f"DROP VIEW IF EXISTS {CONSUMER}")
    return check.verdict(
        "each opted-in model is written to a candidate, audited as the table consumers "
        "would get, and promoted in place (dependents and grants intact) or quarantined "
        "on its own; nothing downstream is built from a quarantined model and no "
        "published event is emitted."
    )


if __name__ == "__main__":
    sys.exit(main())
