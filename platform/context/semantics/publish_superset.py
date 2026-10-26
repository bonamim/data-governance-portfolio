#!/usr/bin/env python3
"""Adapter: publish the Ossie semantic models to Superset (ADR-0010, M6).

Reads every ``domains/*/semantics/*.semantic_model.yaml`` and, via the Superset API, ensures the
warehouse connection, a dataset per semantic dataset, and its metrics — generated from the one
governed Ossie definition, not hand-typed in the BI tool. Idempotent.

Run: ``make bi-publish`` (needs Superset up — ``make bi-up``).
"""

from __future__ import annotations

import argparse
import os
import re
import sys

from dgp_platform_semantics import ossie
from dgp_platform_semantics.superset import Superset

DB_NAME = "analytics (warehouse)"
# The warehouse connection, as the consumer role (output ports only). Engine-agnostic: point it at
# Postgres, Snowflake and so on — the metric SQL is ANSI, so it ports.
DB_URI = os.environ.get("SUPERSET_WAREHOUSE_URI")


def _ensure_database(superset: Superset) -> int:
    db_id = superset.find("database", "database_name", DB_NAME)
    if db_id:
        return db_id
    if not DB_URI:
        raise SystemExit("SUPERSET_WAREHOUSE_URI is required to create the warehouse connection.")
    return superset.post("database/", {"database_name": DB_NAME, "sqlalchemy_uri": DB_URI})["id"]


def _norm(expr: str) -> str:
    return re.sub(r"\s+", "", expr or "").upper()


def _metrics(spec: dict, dataset: str) -> list[dict]:
    """The model's metrics as Superset metrics. The expression comes from the shared reader
    (``SUM(sales_performance.x)`` -> ``SUM(x)`` in Superset's dataset scope), so BI and the AI
    consumer never compile a metric separately."""
    return [
        {
            "metric_name": m["name"],
            "expression": ossie.superset_metric_expr(m, dataset),
            "verbose_name": m["name"],
            "description": m.get("description", ""),
        }
        for m in ossie.metrics(spec)
    ]


def _published_datasets() -> list[tuple[dict, dict, str, str]]:
    """(model, dataset, schema, table) of every dataset sourced from ``<db>.<schema>.<table>``."""
    out = []
    for path in ossie.models():
        spec = ossie.load_compiled(path)
        for dataset in ossie.datasets(spec):
            parts = str(dataset.get("source", "")).split(".")
            if len(parts) == 3:
                out.append((spec, dataset, parts[1], parts[2]))
    return out


def _ensure_dataset(
    superset: Superset, db_id: int, schema: str, table: str, metrics: list[dict]
) -> int:
    ds_id = superset.find("dataset", "table_name", table)
    if ds_id is None:
        body = {"database": db_id, "schema": schema, "table_name": table}
        ds_id = superset.post("dataset/", body, timeout=60)["id"]
    # Carry each existing metric's id so the PUT updates it in place: Superset rejects a metric
    # that matches an existing name without an id ("already exist"), so a re-publish would 422.
    ids = {m["metric_name"]: m["id"] for m in superset.dataset(ds_id).get("metrics") or []}
    merged = [{**m, "id": ids[m["metric_name"]]} if m["metric_name"] in ids else m for m in metrics]
    superset.put(f"dataset/{ds_id}", {"metrics": merged}, timeout=60)
    return ds_id


def _publish(superset: Superset) -> int:
    db_id = _ensure_database(superset)
    for spec, dataset, schema, table in _published_datasets():
        metrics = _metrics(spec, dataset["name"])
        _ensure_dataset(superset, db_id, schema, table, metrics)
        print(
            f"  published dataset {schema}.{table} with {len(metrics)} metric(s) from "
            f"{spec.get('name')}"
        )
    print("Semantic models published to Superset.")
    return 0


def _drift(superset: Superset) -> list[str]:
    """Where Superset's metrics differ from the Ossie models (e.g. edited in the UI)."""
    drift: list[str] = []
    for spec, dataset, _, table in _published_datasets():
        expected = {m["metric_name"]: m["expression"] for m in _metrics(spec, dataset["name"])}
        published = superset.dataset_by_table(table)
        if published is None:
            drift.append(f"{table}: not published to Superset — run `make bi-publish`")
            continue
        actual = {m["metric_name"]: m.get("expression", "") for m in published.get("metrics") or []}
        for name, expr in expected.items():
            if name not in actual:
                drift.append(f"{table}.{name}: missing in Superset")
            elif _norm(actual[name]) != _norm(expr):
                drift.append(
                    f"{table}.{name}: expression drifted (Ossie '{expr}' vs Superset "
                    f"'{actual[name]}')"
                )
        drift += [
            f"{table}.{name}: extra metric in Superset, not in the Ossie model"
            for name in actual.keys() - expected.keys()
        ]
    return drift


def _check(superset: Superset) -> int:
    if drift := _drift(superset):
        print("Semantic drift FAILED — Superset does not match the Ossie models:")
        for d in drift:
            print(f"  - {d}")
        return 1
    print("Semantic drift OK: Superset metrics match the Ossie models.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish/verify Ossie models in Superset.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if Superset drifted from the Ossie models (don't publish)",
    )
    args = parser.parse_args()
    if not ossie.models():
        print("No semantic models.")
        return 0
    superset = Superset()
    return _check(superset) if args.check else _publish(superset)


if __name__ == "__main__":
    sys.exit(main())
