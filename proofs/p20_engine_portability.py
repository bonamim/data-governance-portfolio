#!/usr/bin/env python3
"""Proof P20 — engine portability: the same contracts, products and numbers on SQLMesh (ADR-0026).

The four output ports are rebuilt by SQLMesh (``portability/sqlmesh``, native models, schemas
``sqlmesh_*``) from the same raw tables, and judged by the same ODCS contracts:

  1. one source   SQLMesh's port schemas and audits are generated from the ODCS contracts (no drift)
  2. build        ``sqlmesh plan`` builds all products; the generated audits pass
  3. contracts    ``datacontract test`` — the engine-neutral checker — passes every port contract on
                  BOTH engines' tables
  4. same data    each port is identical row for row on both engines (EXCEPT ALL, both ways)
  5. state        (the ADR-0006 comparison) SQLMesh records built intervals itself: a re-plan has
                  nothing to do, and restating Mar–Apr 2017 rewrites exactly those rows (as P6 does
                  for dbt microbatch)
  6. audits       a bad row restated into Jan 2017 fails SQLMesh's blocking audit — but the rows are
                  already in its production table (a restatement writes in place, then audits). dbt
                  + the M9 audit view quarantines the same batch before it is merged (P15).

Needs the platform (``make up``). Run: ``make prove-p20``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import psycopg
import yaml
from dgp_platform_quality import odcs

REPO = Path(__file__).resolve().parents[1]
PROJECT = REPO / "portability" / "sqlmesh"
PORTS = {
    "customer/contracts/customers": "customer_silver.customers",
    "customer_experience/contracts/reviews": "customer_experience_silver.reviews",
    "sales/contracts/orders": "sales_silver.orders",
    "sales/contracts/sales_performance": "sales_gold.sales_performance",
}
ORDERS = "sqlmesh_sales_silver.orders"

failures: list[str] = []


def check(ok: bool, what: str, detail: str = "") -> bool:
    print(f"  {'OK  ' if ok else 'FAIL'} {what}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(what)
    return ok


def sqlmesh(*args: str) -> tuple[int, str]:
    r = subprocess.run(
        ["sqlmesh", *args, "--no-prompts"], cwd=PROJECT, capture_output=True, text=True
    )
    return r.returncode, r.stdout + r.stderr


def db(query: str, *params, superuser: bool = False):
    user, pw = (
        (os.environ.get("ANALYTICS_SUPERUSER", "postgres"), "ANALYTICS_SUPERUSER_PASSWORD")
        if superuser
        else ("sqlmesh_role", "SQLMESH_ROLE_PASSWORD")
    )
    with psycopg.connect(
        f"host=localhost port={os.environ.get('ANALYTICS_PORT', '5433')} "
        f"dbname={os.environ.get('ANALYTICS_DB', 'analytics')} user={user} "
        f"password={os.environ[pw]}",
        autocommit=True,
    ) as c:
        cur = c.execute(query, params)
        return cur.fetchall() if cur.description else []


def engine_contract(path: str, schema: str, engine: str) -> dict:
    """The port's contract, resolved, pointed at one engine's schema. Resolved here because the copy
    is tested from a temp dir, where its relative links would not resolve."""
    doc = odcs.load(REPO / "domains" / f"{path}.odcs.yaml")
    doc["servers"] = [
        {
            "server": engine,
            "type": "postgres",
            "host": "localhost",
            "port": int(os.environ.get("ANALYTICS_PORT", "5433")),
            "database": os.environ.get("ANALYTICS_DB", "analytics"),
            "schema": schema,
        }
    ]
    return doc


def contract_test(path: str, schema: str, engine: str, tmp: Path) -> tuple[bool, str]:
    doc = engine_contract(path, schema, engine)
    f = tmp / f"{engine}_{path.rsplit('/', 1)[1]}.odcs.yaml"
    f.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    user, pw = (
        ("sqlmesh_role", os.environ["SQLMESH_ROLE_PASSWORD"])
        if engine == "sqlmesh"
        else (
            os.environ.get("ANALYTICS_SUPERUSER", "postgres"),
            os.environ["ANALYTICS_SUPERUSER_PASSWORD"],
        )
    )
    r = subprocess.run(
        ["datacontract", "test", "--no-inline-references", "--server", engine, str(f)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "DATACONTRACT_POSTGRES_USERNAME": user,
            "DATACONTRACT_POSTGRES_PASSWORD": pw,
        },
    )
    ok = "Data contract is valid" in r.stdout
    return ok, "" if ok else r.stdout.split("errors:")[-1].strip()[:300]


def physical(view: str) -> str:
    """The versioned table behind a SQLMesh production view (its virtual layer)."""
    return db(
        "SELECT DISTINCT d.refobjid::regclass::text FROM pg_depend d JOIN pg_rewrite r"
        " ON r.oid = d.objid WHERE r.ev_class = %s::regclass AND d.refobjid <> r.ev_class"
        " AND d.refobjsubid > 0",
        view,
    )[0][0]


def versions(start: str, end: str) -> dict[str, tuple[str, bool]]:
    """order_id -> (row version, inside the window), read from the physical table."""
    rows = db(
        f"SELECT order_id, xmin::text, order_purchase_timestamp >= %s AND "
        f"order_purchase_timestamp < %s FROM {physical(ORDERS)}",
        start,
        end,
    )
    return {r[0]: (r[1], r[2]) for r in rows}


def main() -> int:
    print("1. One source: SQLMesh's port schemas and audits come from the ODCS contracts")
    r = subprocess.run(
        [sys.executable, str(PROJECT / "sync_contracts.py"), "--check"],
        capture_output=True,
        text=True,
    )
    check(r.returncode == 0, "no drift between contracts and SQLMesh models", r.stdout.strip())

    print("2. Build: SQLMesh plans and builds every product")
    rc, out = sqlmesh("plan", "--auto-apply")
    check(
        rc == 0,
        "sqlmesh plan applied to prod",
        "audits passed" if "audits passed" in out else out.strip().splitlines()[-1],
    )

    print("3. Contracts: the same ODCS contracts pass on both engines")
    with tempfile.TemporaryDirectory() as t:
        for path, port in PORTS.items():
            schema, _ = port.split(".")
            for engine, prefix in (("dbt", ""), ("sqlmesh", "sqlmesh_")):
                ok, why = contract_test(path, prefix + schema, engine, Path(t))
                check(ok, f"{engine:7} {port}", why)

    print("4. Same data: every port identical row for row")
    for port in PORTS.values():
        only = [
            db(
                f"SELECT count(*) FROM (SELECT * FROM {a} EXCEPT ALL SELECT * FROM {b}) x",
                superuser=True,
            )[0][0]
            for a, b in ((port, f"sqlmesh_{port}"), (f"sqlmesh_{port}", port))
        ]
        rows = db(f"SELECT count(*) FROM {port}", superuser=True)[0][0]
        check(only == [0, 0], f"{port}", f"{rows} rows, differences {only[0]} / {only[1]}")

    print("5. State: SQLMesh tracks its intervals; a restatement rewrites only its range")
    rc, out = sqlmesh("plan", "--auto-apply")
    check(rc == 0 and "No changes to plan" in out, "re-plan: nothing to do (intervals recorded)")
    v0 = versions("2017-03-01", "2017-05-01")
    rc, out = sqlmesh(
        "plan",
        "--restate-model",
        ORDERS,
        "--start",
        "2017-03-01",
        "--end",
        "2017-04-30",
        "--auto-apply",
    )
    v1 = versions("2017-03-01", "2017-05-01")
    moved = {k for k in v1 if k in v0 and v1[k][0] != v0[k][0]}
    window = {k for k, (_, w) in v1.items() if w}
    check(
        rc == 0 and moved == window,
        "restated exactly Mar–Apr 2017",
        f"{len(moved)} rows rewritten, {len(window)} in the window, {len(moved - window)} outside",
    )

    print("6. Audits: a bad batch on restatement (comparison with dbt + the M9 audit view)")
    db(
        "INSERT INTO sales_raw.orders (order_id, customer_id, order_status, "
        "order_purchase_timestamp) VALUES ('p20bad', NULL, 'delivered', '2017-01-15 10:00:00')",
        superuser=True,
    )
    try:
        rc, out = sqlmesh(
            "plan",
            "--restate-model",
            ORDERS,
            "--start",
            "2017-01-01",
            "--end",
            "2017-01-31",
            "--auto-apply",
        )
        seen = db(f"SELECT count(*) FROM {ORDERS} WHERE order_id = 'p20bad'")[0][0]
        check(
            rc != 0 and "'not_null' audit error" in out, "SQLMesh's blocking audit fails the plan"
        )
        check(
            seen == 1,
            "observed: the bad row is already in SQLMesh's prod table (written, then "
            "audited) — dbt's audit view quarantines it before the merge (P15)",
        )
    finally:
        db("DELETE FROM sales_raw.orders WHERE order_id = 'p20bad'", superuser=True)
        sqlmesh(
            "plan",
            "--restate-model",
            ORDERS,
            "--start",
            "2017-01-01",
            "--end",
            "2017-01-31",
            "--auto-apply",
        )
    check(
        db(f"SELECT count(*) FROM {ORDERS} WHERE order_id = 'p20bad'")[0][0] == 0,
        "cleaned up: restated without the bad row",
    )

    if failures:
        print(f"\nP20 FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print(
        "\nP20 proven: the contracts are the portable interface — a second, natively stateful"
        " engine builds the same products to the same contracts with identical data; the"
        " differences that matter (type inference, audit timing on restatement) are recorded."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
