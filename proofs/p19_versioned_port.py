#!/usr/bin/env python3
"""Proof P19 — a versioned output port: release v2 beside v1, migrate, retire (ADR-0019 §3).

``customer.customers`` needs a breaking change (drop ``customer_zip_code_prefix``) but a consumer
can't cut over atomically. Worked end to end on the live platform, then reverted:

  1. release   ``customers_v2`` (contract 2.0.0, ``version_of: customers``) is added and v1 is
               deprecated with a sunset date. Nothing breaks, so the breaking-change gate passes
               WITHOUT approvals; every governance check passes; v1 is still read by sales (notice)
  2. both      v1 and v2 are built side by side; v2 is masked and granted like any port; sales
               still builds on v1, which dbt now flags as a deprecated reference
  3. sunset    after the sunset date CI fails and names sales as the consumer still on v1
  4. too early retiring v1 while sales reads it is BLOCKED by the gate
  5. migrate   sales moves to v2 (ref + input) and rebuilds with the same numbers; retiring v1 then
               needs only the declared consumers' sign-off — with it, the gate passes

Run: ``make prove-p19`` (needs the platform: ``make up``).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import psycopg
import yaml

REPO = Path(__file__).resolve().parents[1]
C = REPO / "domains" / "customer"
V1_CONTRACT, V1_DEF = C / "contracts/customers.odcs.yaml", C / "data_products/customers.yaml"
V1_SQL = C / "dbt/models/silver/customers.sql"
V2_CONTRACT, V2_DEF = C / "contracts/customers_v2.odcs.yaml", C / "data_products/customers_v2.yaml"
V2_SQL = C / "dbt/models/silver/customers_v2.sql"
S = REPO / "domains" / "sales"
SALES_SQL = S / "dbt/models/gold/sales_performance.sql"
SALES_DEF = S / "data_products/sales_performance.yaml"
APPROVAL = REPO / "governance/approvals/customers-retire.yaml"
NEW = [V2_CONTRACT, V2_DEF, V2_SQL, APPROVAL]
DROPPED = "customer_zip_code_prefix"
SUNSET = date.today() + timedelta(days=60)

failures: list[str] = []


def check(ok: bool, what: str, detail: str = "") -> bool:
    print(f"  {'OK  ' if ok else 'FAIL'} {what}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(what)
    return ok


def run(*cmd: str, **env: str) -> tuple[int, str]:
    r = subprocess.run(
        list(cmd), cwd=REPO, capture_output=True, text=True, env={**os.environ, **env}
    )
    return r.returncode, r.stdout + r.stderr


def py(script: str, *args: str, **env: str) -> tuple[int, str]:
    return run(sys.executable, str(REPO / script), *args, **env)


def gate() -> tuple[int, str]:
    return py("governance/policies/check_breaking_changes.py", BASE_REF="HEAD")


def sql(query: str, user: str = "analytics_reader", pw: str = "ANALYTICS_READER_PASSWORD"):
    with psycopg.connect(
        f"host=localhost port={os.environ.get('ANALYTICS_PORT', '5433')} "
        f"dbname={os.environ.get('ANALYTICS_DB', 'analytics')} user={user} "
        f"password={os.environ[pw]}"
    ) as c:
        return c.execute(query).fetchall()


def superuser(query: str) -> None:
    run(
        "docker",
        "exec",
        "governance-platform-postgres-analytics-1",
        "psql",
        "-U",
        os.environ.get("ANALYTICS_SUPERUSER", "postgres"),
        "-d",
        os.environ.get("ANALYTICS_DB", "analytics"),
        "-c",
        query,
    )


def dump(path: Path, doc: dict, header: str = "") -> None:
    path.write_text(header + yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def step_release() -> None:
    print(f"1. Release: customers_v2 (drops {DROPPED}) beside v1, which is deprecated")
    v1 = yaml.safe_load(V1_CONTRACT.read_text(encoding="utf-8"))
    v2 = yaml.safe_load(V1_CONTRACT.read_text(encoding="utf-8"))
    v2.update(version="2.0.0", id="customer.customers_v2", name="Customer - Customers v2")
    v2["schema"][0]["name"] = "customers_v2"
    v2["schema"][0]["properties"] = [
        p for p in v2["schema"][0]["properties"] if p["name"] != DROPPED
    ]
    dump(V2_CONTRACT, v2, "# customer.customers v2 (ADR-0019 §3) — P19 worked example.\n")
    v1["status"] = "deprecated"
    v1.setdefault("customProperties", []).append(
        {"property": "sunsetDate", "value": SUNSET.isoformat()}
    )
    dump(V1_CONTRACT, v1)
    d2 = yaml.safe_load(V1_DEF.read_text(encoding="utf-8"))
    d2.update(
        name="customers_v2", version_of="customers", display_name=d2["display_name"] + " (v2)"
    )
    d2["output_ports"] = [
        {"kind": "table", "contract": "domains/customer/contracts/customers_v2.odcs.yaml"}
    ]
    dump(V2_DEF, d2)
    V2_SQL.write_text(
        "\n".join(
            line for line in V1_SQL.read_text(encoding="utf-8").splitlines() if DROPPED not in line
        )
        + "\n",
        encoding="utf-8",
    )
    rc, out = run("make", "-s", "dbt-ports")
    check(rc == 0, "contracts synced into dbt", out.strip().splitlines()[-1] if out else "")
    ports = (C / "dbt/models/silver/_ports.yml").read_text(encoding="utf-8")
    check(
        f"deprecation_date: '{SUNSET} 00:00:00+00:00'" in ports,
        "v1's sunset became dbt's deprecation_date",
    )
    rc, out = gate()
    check(
        rc == 0 and "customers_v2: new output-port contract" in out and "BLOCKED" not in out,
        "the gate passes with NO approval: nothing a consumer reads has changed",
    )
    if failures and failures[-1].startswith("the gate passes"):
        print(out[-1500:])
    rc, out = py("governance/policies/check_port_versions.py")
    check(
        rc == 0 and "still read by sales.sales_performance" in out,
        "version policy OK, noting sales still reads v1",
        next((line.strip() for line in out.splitlines() if "notice" in line), ""),
    )
    for policy in ("check_output_ports.py", "check_data_products.py"):
        rc, out = py(f"governance/policies/{policy}")
        check(rc == 0, f"{policy} passes", out.strip().splitlines()[-1] if out else "")


def step_both() -> None:
    print("2. Both versions served: v2 built, masked and granted; sales still on v1")
    rc, out = py("platform/core/transform/publish.py", "--domain", "customer", "--no-state")
    check(rc == 0, "customer published (v1 and v2)")
    cols = [
        r[0]
        for r in sql(
            "SELECT column_name FROM information_schema.columns WHERE "
            "table_schema = 'customer_masked' AND table_name = 'customers_v2'"
        )
    ]
    check(
        bool(cols) and DROPPED not in cols,
        "consumers read v2 through its masking view",
        ", ".join(cols),
    )
    masked = sql(
        "SELECT count(*), count(*) FILTER (WHERE length(customer_unique_id) = 32) "
        "FROM customer_masked.customers_v2"
    )[0]
    check(
        masked[0] > 0 and masked[0] == masked[1],
        "its PII is masked for them",
        f"{masked[1]}/{masked[0]} hashed",
    )
    check(
        sql("SELECT count(*) FROM customer_masked.customers")[0][0] == masked[0],
        "v1 is still served unchanged",
    )
    rc, out = py("platform/core/transform/publish.py", "--domain", "sales", "--no-state")
    check(rc == 0, "sales still builds on v1")
    warned = "deprecat" in out.lower()
    print(
        f"  {'OK  ' if warned else 'note'} dbt flags the deprecated reference"
        + ("" if warned else " — not surfaced through dbt-loom here")
    )


def step_sunset() -> None:
    print("3. Sunset passed: CI fails and names who has not migrated")
    rc, out = py(
        "governance/policies/check_port_versions.py",
        DGP_TODAY=(SUNSET + timedelta(days=1)).isoformat(),
    )
    check(
        rc != 0 and "retire it after migrating sales.sales_performance" in out,
        "fails, naming sales.sales_performance",
        next((line.strip() for line in out.splitlines() if "sunset" in line), ""),
    )


def step_retire_too_early(saved: dict[Path, str]) -> None:
    print("4. Retiring v1 while sales still reads it: blocked")
    for f in (V1_CONTRACT, V1_DEF, V1_SQL):
        f.unlink()
    rc, out = gate()
    check(rc != 0 and "still read by sales.sales_performance" in out, "the gate blocks it")


def step_migrate_and_retire() -> None:
    print("5. Migrate sales to v2, then retire v1 with its consumers' sign-off")
    SALES_SQL.write_text(
        SALES_SQL.read_text(encoding="utf-8").replace(
            "ref('dgp_customer', 'customers')", "ref('dgp_customer', 'customers_v2')"
        ),
        encoding="utf-8",
    )
    SALES_DEF.write_text(
        SALES_DEF.read_text(encoding="utf-8").replace(
            "- customer.customers\n", "- customer.customers_v2\n"
        ),
        encoding="utf-8",
    )
    before = sql(
        "SELECT count(*), sum(total_revenue)::text FROM sales_gold.sales_performance",
        "sales_role",
        "SALES_ROLE_PASSWORD",
    )[0]
    rc, _ = py("platform/core/transform/publish.py", "--domain", "sales", "--no-state")
    after = sql(
        "SELECT count(*), sum(total_revenue)::text FROM sales_gold.sales_performance",
        "sales_role",
        "SALES_ROLE_PASSWORD",
    )[0]
    check(rc == 0 and after == before, "sales rebuilt on v2 with the same numbers", str(after))
    rc, out = gate()
    check(
        rc != 0 and "needs approval from: sales" in out,
        "retirement now waits only for the declared consumers' sign-off",
    )
    dump(
        APPROVAL,
        {
            "port": "customers",
            "change": "retire v1 (successor customers_v2)",
            "approved_by": [{"team": "sales", "user": "p19", "pr": 0}],
        },
    )
    rc, out = gate()
    check(rc == 0 and "APPROVED: no data product reads it" in out, "with it, the gate passes")
    rc, out = py("governance/policies/check_port_versions.py")
    check(rc == 0, "the version policy is satisfied")


def restore(saved: dict[Path, str]) -> None:
    print("   restoring the repo and the warehouse")
    for f, text in saved.items():
        f.write_text(text, encoding="utf-8")
    for f in NEW:
        f.unlink(missing_ok=True)
    superuser(
        "DROP VIEW IF EXISTS customer_masked.customers_v2; "
        "DROP VIEW IF EXISTS customer_silver.customers_v2 CASCADE;"
    )
    run("make", "-s", "dbt-ports")
    # The sync generated dbt tests for v2's contract rules.
    shutil.rmtree(C / "dbt/tests/datacontract_cli/customer_customers_v2", ignore_errors=True)
    py("platform/core/transform/publish.py", "--domain", "customer", "--no-state")
    py("platform/core/transform/publish.py", "--domain", "sales", "--no-state")
    run("make", "-s", "dbt-manifests")
    _, status = run("git", "status", "--short", "--", "domains", "governance/approvals")
    check(status.strip() == "", "repo restored", status.strip() or "clean")


def main() -> int:
    saved = {
        f: f.read_text(encoding="utf-8")
        for f in (
            V1_CONTRACT,
            V1_DEF,
            V1_SQL,
            SALES_SQL,
            SALES_DEF,
            C / "dbt/models/silver/_ports.yml",
        )
    }
    try:
        step_release()
        step_both()
        step_sunset()
        step_retire_too_early(saved)
        step_migrate_and_retire()
    finally:
        restore(saved)
    if failures:
        print(f"\nP19 FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print(
        "\nP19 proven: a breaking change ships as a new port version beside the old one — nobody"
        " breaks on release, the old version is retired only after its sunset is honoured, its"
        " readers have migrated and its consumers have signed off."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
