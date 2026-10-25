#!/usr/bin/env python3
"""Proof P16 — dbt state + Slim CI: a change builds only what it touched (ADR-0015 §3).

The baseline is the manifest every successful publish saves to ``platform-state``.

  1. no change      every domain plans 0 models
  2. leaf change    an edit to sales_performance plans exactly that model; the build lands in the
                    sales_ci sandbox and its ref to orders is DEFERRED to the published
                    sales_silver.orders (not rebuilt); the published port is untouched
  3. upstream edit  an edit to brz_orders plans brz_orders and its descendants (orders,
                    sales_performance) — nothing else in the project
  4. the CI path    the same selection against a git ref with no warehouse (what GitHub Actions
                    runs on every pull request)

Edits are temporary and reverted. Run: ``make prove-p16``.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import psycopg

REPO = Path(__file__).resolve().parents[1]
MODELS = REPO / "domains" / "sales" / "dbt" / "models"
LEAF, UPSTREAM = MODELS / "gold" / "sales_performance.sql", MODELS / "bronze" / "brz_orders.sql"

failures: list[str] = []


def check(ok: bool, what: str, detail: str = "") -> bool:
    print(f"  {'OK  ' if ok else 'FAIL'} {what}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(what)
    return ok


def slim_ci(*args: str) -> tuple[int, dict[str, list[str]], str]:
    r = subprocess.run(
        [sys.executable, str(REPO / "platform" / "core" / "transform" / "slim_ci.py"), *args],
        cwd=REPO, capture_output=True, text=True,
    )  # fmt: skip
    out = r.stdout + r.stderr
    plan = {
        m.group(1): [x for x in m.group(2).split(", ") if x != "(none)"]
        for m in re.finditer(r"^(\w+): \d+ model\(s\) to build: (.*)$", out, re.M)
    }
    return r.returncode, plan, out


def sql(query: str):
    with psycopg.connect(
        f"host={os.environ.get('ANALYTICS_HOST', 'localhost')} "
        f"port={os.environ.get('ANALYTICS_PORT', '5433')} "
        f"dbname={os.environ.get('ANALYTICS_DB', 'analytics')} "
        f"user=sales_role password={os.environ['SALES_ROLE_PASSWORD']}"
    ) as c:
        return c.execute(query).fetchall()


def edited(path: Path):
    """Context manager: append a comment to a model (changes its checksum), revert after."""

    class _Edit:
        def __enter__(self):
            self.original = path.read_text(encoding="utf-8")
            path.write_text(self.original + "\n-- p16 probe\n", encoding="utf-8")

        def __exit__(self, *exc):
            path.write_text(self.original, encoding="utf-8")

    return _Edit()


def main() -> int:
    print("1. No change: nothing to build")
    rc, plan, out = slim_ci("--plan-only")
    if rc or not plan:
        print(out[-1500:])
        sys.exit("slim CI could not plan — is there a published baseline? run `make publish`")
    check(all(not v for v in plan.values()), "every domain plans 0 models", str(plan))

    print("2. Leaf change: built alone in the sandbox, its upstream deferred to what is published")
    before = sql("SELECT 'sales_gold.sales_performance'::regclass::oid, xmin::text"
                 " FROM sales_gold.sales_performance LIMIT 1")  # fmt: skip
    with edited(LEAF):
        rc, plan, out = slim_ci("--domain", "sales")
    check(plan.get("sales") == ["sales_performance"], "plans exactly sales_performance",
          str(plan.get("sales")))  # fmt: skip
    check(rc == 0, "built and tested in the sandbox", "")
    rows = sql("SELECT count(*) FROM sales_ci.sales_performance")[0][0]
    check(rows > 0, "sales_ci.sales_performance built", f"{rows} rows")
    check(not sql("SELECT 1 FROM pg_class WHERE relnamespace = 'sales_ci'::regnamespace"
                  " AND relname IN ('orders', 'brz_orders')"),
          "orders was not rebuilt in the sandbox")  # fmt: skip
    compiled = (REPO / "domains/sales/dbt/target/ci/run/dgp_sales/models/gold/"
                "sales_performance.sql").read_text(encoding="utf-8")  # fmt: skip
    check('"sales_silver"."orders"' in compiled,
          "ref('orders') deferred to the published sales_silver.orders")  # fmt: skip
    after = sql("SELECT 'sales_gold.sales_performance'::regclass::oid, xmin::text"
                " FROM sales_gold.sales_performance LIMIT 1")  # fmt: skip
    check(after == before, "the published port was not touched")

    print("3. Upstream change: the model and its descendants, nothing else")
    with edited(UPSTREAM):
        rc, plan, _ = slim_ci("--domain", "sales", "--plan-only")
    check(plan.get("sales") == ["brz_orders", "orders", "sales_performance"],
          "plans brz_orders + descendants only", str(plan.get("sales")))  # fmt: skip

    print("4. The CI path: same selection against a git ref, no warehouse")
    # Relative to HEAD the working tree may already differ; the edit must add exactly its model.
    git = ("--domain", "sales", "--plan-only", "--baseline", "git:HEAD")
    rc0, clean, out = slim_ci(*git)
    with edited(LEAF):
        rc, plan, out = slim_ci(*git)
    expected = sorted({*clean.get("sales", []), "sales_performance"})
    check(rc0 == 0 and rc == 0 and plan.get("sales") == expected,
          "git baseline plans the edited model on top of the tree's own changes",
          f"{clean.get('sales')} -> {plan.get('sales')}")  # fmt: skip
    if rc:
        print(out[-1500:])

    if failures:
        print(f"\nP16 FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print(
        "\nP16 proven: against the published state a change builds only the models it touched (and"
        " their descendants) in a sandbox, reading everything else from what is published."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
