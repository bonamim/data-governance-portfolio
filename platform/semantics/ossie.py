#!/usr/bin/env python3
"""Shared Apache Ossie reader — the one place a semantic model becomes SQL (ADR-0010).

Both consumers of the semantic layer translate Ossie *here*, so there is no second parser that
could drift: the Superset adapter (``generate_superset.py``, M6) and the governed semantic MCP
server (``ai/semantic-mcp``, M7) call the same functions. "One governed definition per metric" is
therefore enforced in code, not only by convention — a metric yields the same SQL whoever asks.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DIALECT = "ANSI_SQL"


def models(domain: str | None = None) -> list[Path]:
    """Every domain's Ossie semantic model, sorted — or only ``domain``'s when given."""
    pattern = (
        f"domains/{domain}/semantics/*.semantic_model.yaml"
        if domain
        else ("domains/*/semantics/*.semantic_model.yaml")
    )
    return sorted(REPO_ROOT.glob(pattern))


def domain_of(path: Path) -> str:
    """The owning domain of a semantic model, from its path (domains/<domain>/semantics/...)."""
    return path.relative_to(REPO_ROOT / "domains").parts[0]


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def datasets(spec: dict) -> list[dict]:
    return spec.get("datasets") or []


def metrics(spec: dict) -> list[dict]:
    return spec.get("metrics") or []


def fields(dataset: dict) -> list[dict]:
    return dataset.get("fields") or []


def metric_by_name(spec: dict, name: str) -> dict | None:
    return next((m for m in metrics(spec) if m.get("name") == name), None)


def field_by_name(dataset: dict, name: str) -> dict | None:
    return next((f for f in fields(dataset) if f.get("name") == name), None)


def _dialect_expr(node: dict | None) -> str:
    """The ANSI_SQL expression of an Ossie field/metric node (``expression.dialects[]``)."""
    for d in ((node or {}).get("expression") or {}).get("dialects") or []:
        if d.get("dialect") == DIALECT:
            return d.get("expression", "")
    return ""


def ansi_metric_expr(metric: dict) -> str:
    """The metric's ANSI expression, verbatim — e.g. ``SUM(sales_performance.total_revenue)``."""
    return _dialect_expr(metric)


def superset_metric_expr(metric: dict, dataset: str) -> str:
    """Metric expression for Superset, where metrics run in the dataset's own column scope: strip
    the ``<dataset>.`` qualifier so ``SUM(sales_performance.x)`` -> ``SUM(x)``."""
    return re.sub(rf"\b{re.escape(dataset)}\.", "", ansi_metric_expr(metric))


def metric_sql(
    spec: dict, dataset: dict, metric_names: list[str], dimensions: list[str] = ()
) -> str:
    """Governed ANSI SQL for the requested metrics (optionally grouped by dimensions).

    The source table is aliased ``AS <dataset>`` so the metric expressions
    (``SUM(<dataset>.col)``) resolve verbatim — the same strings the governance check and the
    Superset projection read. This is the one translation both the BI and AI consumers go through.
    """
    name = dataset["name"]
    source = dataset["source"]
    selects: list[str] = []
    group: list[str] = []
    for dim in dimensions:
        expr = _dialect_expr(field_by_name(dataset, dim)) or dim
        selects.append(f"{expr} AS {dim}")
        group.append(expr)
    for mname in metric_names:
        metric = metric_by_name(spec, mname)
        if metric is None:
            model = spec.get("name")
            raise KeyError(f"metric '{mname}' is not defined in semantic model '{model}'")
        selects.append(f"{ansi_metric_expr(metric)} AS {mname}")
    sql = f"SELECT {', '.join(selects)} FROM {source} AS {name}"
    if group:
        sql += f" GROUP BY {', '.join(group)}"
    return sql
