"""The Apache Ossie reader — the one place a semantic model becomes SQL (ADR-0010).

Everything that consumes the semantic layer reads Ossie through this module: the Superset publisher
(``publish_superset.py``), the governed semantic agents (``dgp_ai``), the bundle build
(``build_ossie.py``) and the governance checks. There is no second parser to drift, so "one
governed definition per metric" holds in code, not just by convention: a metric compiles to the
same SQL whoever asks.

A semantic model stays a plain mapping, exactly as Ossie defines it, so it can be validated against
Ossie's schema and written back out unchanged; the functions below are the accessors.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import re
from collections.abc import Sequence
from functools import cache
from pathlib import Path
from types import ModuleType

from dgp_governance import products
from dgp_governance.repo import load_yaml, root

DIALECT = "ANSI_SQL"
VENDOR = Path(__file__).resolve().parent / "vendor" / "ossie"
ONTOLOGY_SCHEMA = "ontology/ontology.json"
MAPPING_SCHEMA = "ontology/mapping.json"
MODEL_SCHEMA = "core-spec/ossie-schema.json"

# A column reference inside an expression: ``<dataset>.<column>`` -> ``column``.
_COLUMN_REF = re.compile(r"\b\w+\.(\w+)\b")


# Locating and loading models.


def models(domain: str | None = None) -> list[Path]:
    """Every domain's semantic model, sorted — or only ``domain``'s when given."""
    return sorted(root().glob(f"domains/{domain or '*'}/semantics/*.semantic_model.yaml"))


def load(path: Path) -> dict:
    """A semantic model as its domain wrote it (what the governance checks judge)."""
    return load_yaml(path)


def load_compiled(path: Path) -> dict:
    """A semantic model as consumers see it: as written, then compiled (``compile_model``)."""
    return compile_model(load(path))


def compile_model(spec: dict) -> dict:
    """A complete, self-contained Ossie document (ADR-0027): a copy of the model in which every
    field that is a contract column gets the column's description and synonyms from its output
    port's resolved contract (inherited from its data element).

    The author writes a column's meaning once, in the data element; every consumer — this
    platform's readers, a bundle deployed to Snowflake, any other Ossie tool — finds it where
    Ossie expects it (``description``, ``ai_context.synonyms``). Derived fields and metrics keep
    their own."""
    out = copy.deepcopy(spec)
    for dataset in datasets(out):
        port = products.output_port(source_table(dataset))
        columns = {c["name"]: c for c in port.columns} if port else {}
        for item in fields(dataset):
            column = columns.get(item.get("name"))
            if column:
                _inherit(item, column)
    return out


def _inherit(item: dict, column: dict) -> None:
    """Fill a field's description and synonyms from its contract column, where it has none."""
    if not str(item.get("description") or "").strip() and column.get("description"):
        item["description"] = " ".join(str(column["description"]).split())
    synonyms = [s["synonym"] for s in column.get("synonyms") or [] if s.get("synonym")]
    context = item.get("ai_context")
    if synonyms and context is None:
        item["ai_context"] = {"synonyms": synonyms}
    elif synonyms and isinstance(context, dict) and "synonyms" not in context:
        context["synonyms"] = synonyms


# Validation, with Ossie's own validator (vendored unchanged, vendor/ossie/).


@cache
def _validator() -> ModuleType:
    """Ossie's validator script, loaded once. It is a standalone script, not a package, and it is
    vendored unchanged, so it is loaded from its file rather than imported."""
    spec = importlib.util.spec_from_file_location(
        "ossie_validate", VENDOR / "validation" / "validate.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@cache
def _schema(name: str) -> dict:
    return json.loads((VENDOR / name).read_text(encoding="utf-8"))


def validate(doc: dict, schema: str = ONTOLOGY_SCHEMA) -> list[str]:
    """Ossie's diagnostics for a parsed document — empty when it is valid. Ontologies by default;
    pass ``MODEL_SCHEMA`` for a semantic model or ``MAPPING_SCHEMA`` for a mapping."""
    return _validator().validate_document(doc, _schema(schema))


# Reading a model.


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


def source_table(dataset: dict) -> str:
    """The table a dataset reads (the last part of its ``source``) — the output port's name."""
    return str(dataset.get("source", "")).split(".")[-1]


def expressions(node: dict | None) -> list[str]:
    """Every dialect's expression of a field or metric (``expression.dialects[]``)."""
    dialects = ((node or {}).get("expression") or {}).get("dialects") or []
    return [d.get("expression") or "" for d in dialects]


def referenced_columns(node: dict | None) -> set[str]:
    """The columns a field or metric expression reads, from its ``<dataset>.<column>`` refs."""
    return {col for expr in expressions(node) for col in _COLUMN_REF.findall(expr)}


# Compiling to SQL.


def _dialect_expr(node: dict | None) -> str:
    """The ANSI_SQL expression of a field or metric."""
    for d in ((node or {}).get("expression") or {}).get("dialects") or []:
        if d.get("dialect") == DIALECT:
            return d.get("expression", "")
    return ""


def ansi_metric_expr(metric: dict) -> str:
    """The metric's ANSI expression, verbatim — e.g. ``SUM(sales_performance.total_revenue)``."""
    return _dialect_expr(metric)


def superset_metric_expr(metric: dict, dataset: str) -> str:
    """The metric expression for Superset, where a metric runs in its dataset's own column scope:
    the ``<dataset>.`` qualifier is dropped, so ``SUM(sales_performance.x)`` becomes ``SUM(x)``."""
    return re.sub(rf"\b{re.escape(dataset)}\.", "", ansi_metric_expr(metric))


def metric_sql(
    spec: dict, dataset: dict, metric_names: Sequence[str], dimensions: Sequence[str] = ()
) -> str:
    """Governed ANSI SQL for the requested metrics, optionally grouped by dimensions.

    The source table is aliased ``AS <dataset>``, so the metric expressions (``SUM(<dataset>.col)``)
    resolve verbatim — the same strings the governance check and the Superset projection read.
    This is the one translation both the BI and the AI consumers go through.
    """
    name = dataset["name"]
    selects: list[str] = []
    group: list[str] = []
    for dim in dimensions:
        expr = _dialect_expr(field_by_name(dataset, dim)) or dim
        selects.append(f"{expr} AS {dim}")
        group.append(expr)
    for metric_name in metric_names:
        metric = metric_by_name(spec, metric_name)
        if metric is None:
            raise KeyError(
                f"metric '{metric_name}' is not defined in semantic model '{spec.get('name')}'"
            )
        selects.append(f"{ansi_metric_expr(metric)} AS {metric_name}")
    sql = f"SELECT {', '.join(selects)} FROM {dataset['source']} AS {name}"
    if group:
        # Ordered by the dimensions, so a monthly breakdown reads in calendar order.
        sql += f" GROUP BY {', '.join(group)} ORDER BY {', '.join(group)}"
    return sql
