#!/usr/bin/env python3
"""Proof P10 — BI and AI agree (ADR-0010, ADR-0020, M7).

For every governed metric, three paths return the same number because all three compile the one
Apache Ossie definition:

  * **AI**     — the semantic MCP server's ``query_metric`` (the agent's only metric path).
  * **BI**     — Superset's published metric, executed *through Superset* against the warehouse.
  * **Ossie**  — a direct control: the shared reader's SQL run straight on the warehouse.

It also checks that Superset's stored metric expression is the governed one, and that the AI path
refuses an ungoverned/PII column, an invented metric and an ungoverned product. Deterministic — no
LLM, no API key. Needs the live platform: warehouse up and a port published to Superset (``make
bi-publish``). Run: ``make prove-p10``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
import requests

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "ai" / "semantic-mcp"))
sys.path.insert(0, str(REPO / "platform" / "semantics"))
import ossie  # noqa: E402  (the shared Ossie->SQL reader BI and AI both compile)
import semantic  # noqa: E402  (the AI path — same reader, run as the consumer role)

SUPERSET = os.environ.get("SUPERSET_URL", "http://localhost:8088")
ADMIN = os.environ.get("SUPERSET_ADMIN", "admin")
ADMIN_PW = os.environ.get("SUPERSET_ADMIN_PASSWORD", "admin")
URI = os.environ.get("SEMANTIC_WAREHOUSE_URI")
TOL = 1e-6

# (product, metrics, dimensions, text the refusal must contain): a PII column as a dimension, an
# invented metric, and a product with no governed metrics (refused toward its owner).
REFUSALS = [
    ("sales_performance", ["total_revenue"], ["customer_unique_id"], "not in the semantic layer"),
    ("sales_performance", ["avg_email_length"], [], "not in the semantic layer"),
    ("customers", ["num_customers"], [], "request_semantic_model"),
]


def _superset() -> requests.Session:
    s = requests.Session()
    r = s.post(
        f"{SUPERSET}/api/v1/security/login",
        json={"username": ADMIN, "password": ADMIN_PW, "provider": "db", "refresh": True},
        timeout=30,
    )
    r.raise_for_status()
    s.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
    s.headers["X-CSRFToken"] = s.get(f"{SUPERSET}/api/v1/security/csrf_token/").json()["result"]
    s.headers["Referer"] = SUPERSET
    return s


def _dataset(s: requests.Session, table: str) -> dict | None:
    r = s.get(f"{SUPERSET}/api/v1/dataset/", params={"page_size": 100}, timeout=30).json()
    for item in r.get("result", []):
        if item.get("table_name") == table:
            return s.get(f"{SUPERSET}/api/v1/dataset/{item['id']}", timeout=30).json()["result"]
    return None


def _bi_value(s: requests.Session, ds_id: int, metric: str) -> float:
    """The number Superset itself computes for the metric (executed against the warehouse)."""
    payload = {
        "datasource": {"id": ds_id, "type": "table"},
        "result_format": "json",
        "result_type": "results",
        "queries": [{"metrics": [metric], "row_limit": 1, "is_timeseries": False}],
    }
    r = s.post(f"{SUPERSET}/api/v1/chart/data", json=payload, timeout=60)
    r.raise_for_status()
    rows = r.json()["result"][0]["data"]
    return float(rows[0][metric])


def _warehouse_value(sql: str) -> float:
    with psycopg.connect(URI) as conn, conn.cursor() as cur:
        cur.execute(sql)
        return float(cur.fetchone()[0])


def _norm(expr: str) -> str:
    return "".join((expr or "").split()).upper()


def main() -> int:
    if not URI:
        print("SEMANTIC_WAREHOUSE_URI is required (the consumer-role warehouse URI).")
        return 2
    s = _superset()
    failures: list[str] = []
    checked = 0

    for path in ossie.models():
        spec = ossie.load(path)
        for ds in ossie.datasets(spec):
            table = str(ds.get("source", "")).split(".")[-1]
            superset_ds = _dataset(s, table)
            if superset_ds is None:
                failures.append(f"{table}: not published to Superset — run `make bi-publish`")
                continue
            ds_id = superset_ds["id"]
            superset_exprs = {
                m["metric_name"]: m.get("expression", "") for m in superset_ds["metrics"]
            }

            for metric in ossie.metrics(spec):
                name = metric["name"]
                checked += 1

                # Definition parity: Superset stores the governed expression.
                want_expr = ossie.superset_metric_expr(metric, ds["name"])
                got_expr = superset_exprs.get(name)
                if got_expr is None:
                    failures.append(f"{table}.{name}: metric missing in Superset")
                    continue
                if _norm(got_expr) != _norm(want_expr):
                    failures.append(
                        f"{table}.{name}: Superset expr '{got_expr}' != governed '{want_expr}'"
                    )

                # Value parity: AI == BI == direct-Ossie control.
                ai = float(semantic.query_metric(ds["name"], [name])["rows"][0][name])
                direct = _warehouse_value(ossie.metric_sql(spec, ds, [name]))
                bi = _bi_value(s, ds_id, name)
                if abs(ai - direct) > TOL or abs(ai - bi) > TOL:
                    failures.append(f"{table}.{name}: disagreement AI={ai} BI={bi} Ossie={direct}")
                else:
                    print(f"  OK {table}.{name}: AI=BI=Ossie={ai}")

    # Refusal: the AI path has no route to an ungoverned column, metric or product.
    for product, metrics, dims, expect in REFUSALS:
        try:
            semantic.query_metric(product, metrics, dims)
            failures.append(f"{product} {metrics} by {dims}: answered, should be refused")
        except ValueError as e:
            if expect not in str(e):
                failures.append(f"{product} {metrics} by {dims}: refused without '{expect}': {e}")
            else:
                print(f"  OK refused {product} {metrics} by {dims}: {expect}")

    if failures:
        print("\nP10 FAILED — BI and AI do not agree:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(
        f"\nP10 OK: {checked} metric(s) agree across AI, BI and the Ossie definition; "
        f"{len(REFUSALS)} ungoverned request(s) refused."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
