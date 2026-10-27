#!/usr/bin/env python3
"""Proof P10: BI and AI agree (ADR-0010, ADR-0020, M7).

For every governed metric, three paths return the same number because all three compile the one
Apache Ossie definition:

  * **AI**:     the semantic MCP server's ``query_metric`` (the agent's only metric path).
  * **BI**:     Superset's published metric, executed *through Superset* against the warehouse.
  * **Ossie**:  a direct control, the shared reader's SQL run straight on the warehouse.

It also checks that Superset's stored metric expression is the governed one, and that the AI path
refuses an ungoverned/PII column, an invented metric and an ungoverned product. Deterministic:
no LLM, no API key. Needs the live platform: warehouse up and a port published to Superset (``make
bi-publish``). Run: ``make prove-p10``.
"""

from __future__ import annotations

import os
import sys

import psycopg
from dgp_ai.semantic import SemanticLayer
from dgp_platform_semantics import ossie
from dgp_platform_semantics.superset import Superset

URI = os.environ.get("SEMANTIC_WAREHOUSE_URI")
TOL = 1e-6

# (product, metrics, dimensions, text the refusal must contain): a PII column as a dimension, an
# invented metric, and a product with no governed metrics (refused toward its owner).
REFUSALS = [
    ("sales_performance", ["total_revenue"], ["customer_unique_id"], "not in the semantic layer"),
    ("sales_performance", ["avg_email_length"], [], "not in the semantic layer"),
    ("customers", ["num_customers"], [], "request_semantic_model"),
]


def _bi_value(superset: Superset, ds_id: int, metric: str) -> float:
    """The number Superset itself computes for the metric (executed against the warehouse)."""
    payload = {
        "datasource": {"id": ds_id, "type": "table"},
        "result_format": "json",
        "result_type": "results",
        "queries": [{"metrics": [metric], "row_limit": 1, "is_timeseries": False}],
    }
    rows = superset.post("chart/data", payload, timeout=60)["result"][0]["data"]
    return float(rows[0][metric])


def _warehouse_value(sql: str) -> float:
    with psycopg.connect(URI) as conn, conn.cursor() as cur:
        cur.execute(sql)
        return float(cur.fetchone()[0])


def _norm(expr: str) -> str:
    return "".join((expr or "").split()).upper()


def _metric_failures(
    s: Superset, ai_layer: SemanticLayer, spec: dict, ds: dict, published: dict
) -> list[str]:
    """One dataset's metrics: Superset stores the governed expression, and AI, BI and a direct
    Ossie control return the same value."""
    failures = []
    table = ossie.source_table(ds)
    stored = {m["metric_name"]: m.get("expression", "") for m in published["metrics"]}
    for metric in ossie.metrics(spec):
        name = metric["name"]
        want_expr = ossie.superset_metric_expr(metric, ds["name"])
        got_expr = stored.get(name)
        if got_expr is None:
            failures.append(f"{table}.{name}: metric missing in Superset")
            continue
        if _norm(got_expr) != _norm(want_expr):
            failures.append(f"{table}.{name}: Superset expr '{got_expr}' != governed '{want_expr}'")
        ai = float(ai_layer.query_metric(ds["name"], [name])["rows"][0][name])
        direct = _warehouse_value(ossie.metric_sql(spec, ds, [name]))
        bi = _bi_value(s, published["id"], name)
        if abs(ai - direct) > TOL or abs(ai - bi) > TOL:
            failures.append(f"{table}.{name}: disagreement AI={ai} BI={bi} Ossie={direct}")
        else:
            print(f"  OK {table}.{name}: AI=BI=Ossie={ai}")
    return failures


def check_agreement(s: Superset, ai_layer: SemanticLayer) -> tuple[list[str], int]:
    """(failures, metrics checked) across every governed dataset."""
    failures, checked = [], 0
    for path in ossie.models():
        spec = ossie.load(path)
        for ds in ossie.datasets(spec):
            published = s.dataset_by_table(ossie.source_table(ds))
            if published is None:
                table = ossie.source_table(ds)
                failures.append(f"{table}: not published to Superset. Run `make bi-publish`")
                continue
            checked += len(ossie.metrics(spec))
            failures += _metric_failures(s, ai_layer, spec, ds, published)
    return failures, checked


def check_refusals(ai_layer: SemanticLayer) -> list[str]:
    """The AI path has no route to an ungoverned column, metric or product."""
    failures = []
    for product, metrics, dims, expect in REFUSALS:
        try:
            ai_layer.query_metric(product, metrics, dims)
            failures.append(f"{product} {metrics} by {dims}: answered, should be refused")
        except ValueError as e:
            if expect not in str(e):
                failures.append(f"{product} {metrics} by {dims}: refused without '{expect}': {e}")
            else:
                print(f"  OK refused {product} {metrics} by {dims}: {expect}")
    return failures


def check_discovery(ai_layer: SemanticLayer) -> list[str]:
    """An ungoverned product is explained (its owner and the next step), not an error."""
    listing = ai_layer.list_metrics("customers")
    if listing.get("governed") is not False or not listing.get("owner_contact"):
        return [f"list_metrics('customers') should name its owner, got {listing}"]
    print(f"  OK list_metrics customers: ungoverned, owner {listing['owner_contact']}")
    return []


def main() -> int:
    if not URI:
        print("SEMANTIC_WAREHOUSE_URI is required (the consumer-role warehouse URI).")
        return 2
    ai_layer = SemanticLayer()
    failures, checked = check_agreement(Superset(), ai_layer)
    failures += check_refusals(ai_layer) + check_discovery(ai_layer)
    if failures:
        print("\nP10 FAILED: BI and AI do not agree:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(
        f"\nP10 OK: {checked} metric(s) agree across AI, BI and the Ossie definition, and "
        f"{len(REFUSALS)} ungoverned request(s) refused."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
