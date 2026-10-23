#!/usr/bin/env python3
"""Shared Apache Ossie reader — the one place a semantic model becomes SQL (ADR-0010).

Both consumers of the semantic layer translate Ossie *here*, so there is no second parser that
could drift: the Superset adapter (``generate_superset.py``, M6) and the governed semantic MCP
server (``ai/semantic-mcp``, M7) call the same functions. "One governed definition per metric" is
therefore enforced in code, not only by convention — a metric yields the same SQL whoever asks.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DIALECT = "ANSI_SQL"
VENDOR = Path(__file__).resolve().parent / "vendor" / "ossie"


def compile(spec: dict) -> dict:  # noqa: A001 — "compile" is the name of the step
    """A semantic model as a complete, self-contained Ossie document (ADR-0027): a copy of the
    authored one in which every field that is a contract column gets the column's description and
    synonyms from its resolved output-port contract (inherited from its data element). The author
    writes them once, in the data element; every consumer — this platform's readers, the bundle
    deployed to Snowflake or another Ossie tool — gets them in the standard places
    (``description``, ``ai_context.synonyms``). Derived fields and metrics keep their own."""
    sys.path.insert(0, str(REPO_ROOT / "platform" / "quality" / "src"))
    from dgp_platform_quality import odcs  # the one contract reader

    out = copy.deepcopy(spec)
    for ds in out.get("datasets") or []:
        table = str(ds.get("source", "")).split(".")[-1]
        columns = {
            prop["name"]: prop
            for contract in REPO_ROOT.glob(f"domains/*/contracts/{table}.odcs.yaml")
            for obj in odcs.load(contract).get("schema") or []
            for prop in obj.get("properties") or []
            if prop.get("name")
        }
        for field in ds.get("fields") or []:
            column = columns.get(field.get("name"))
            if not column:
                continue
            if not str(field.get("description") or "").strip() and column.get("description"):
                field["description"] = " ".join(str(column["description"]).split())
            synonyms = [s["synonym"] for s in column.get("synonyms") or [] if s.get("synonym")]
            context = field.get("ai_context")
            if synonyms and context is None:
                field["ai_context"] = {"synonyms": synonyms}
            elif synonyms and isinstance(context, dict) and "synonyms" not in context:
                context["synonyms"] = synonyms
    return out


def validate(doc: dict, schema: str = "ontology/ontology.json") -> list[str]:
    """Ossie's own validator (vendored unchanged, ``vendor/ossie/``) on a parsed document: its
    diagnostics, empty when the document is valid. Ontologies by default; pass
    ``core-spec/ossie-schema.json`` for a semantic model."""
    spec = importlib.util.spec_from_file_location(
        "ossie_validate", VENDOR / "validation/validate.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.validate_document(doc, json.loads((VENDOR / schema).read_text(encoding="utf-8")))


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
    """A semantic model as authored (what the governance checks judge)."""
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_compiled(path: Path) -> dict:
    """A semantic model as consumers see it: authored, then compiled (``compile``)."""
    return compile(load(path))


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
        # Ordered by the dimensions, so a monthly breakdown reads in calendar order.
        sql += f" GROUP BY {', '.join(group)} ORDER BY {', '.join(group)}"
    return sql
