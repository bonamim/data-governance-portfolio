#!/usr/bin/env python3
"""Semantic-layer governance check (ADR-0010, milestone M6).

Every Apache Ossie semantic model (``domains/*/semantics/*.semantic_model.yaml``) must be built only
on **governed output-port columns**: each dataset's ``source`` resolves to an output port (its ODCS
contract), and every field and metric references a column that exists in that contract. So the
business semantics that Superset and the AI agent compute can't drift from the data contract.

A field is either a contract column (named as the column) or a **derived** field: any other name,
whose expression references only contract columns as ``<dataset>.<column>`` — e.g.
``order_purchase_month`` = ``DATE_TRUNC('month', orders.order_purchase_timestamp)``.

Every dataset, derived field and metric must carry a ``description``: the semantic MCP hands these
to the AI agent verbatim (``list_metrics``), and an agent with no description guesses the meaning
from the name — e.g. counting ``customer_id`` (per order) as people. A field that IS a contract
column must not: its description is the contract's — and its data element's — and the compile
step (``ossie.compile``) puts it into the model every consumer reads, so a column is described in
one place (ADR-0027). The model itself must pass Ossie's own validator (``version: 0.2.0.dev0``).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml
from dgp_platform_contracts import odcs

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "platform" / "context" / "semantics"))
import ossie  # noqa: E402  (the shared Ossie reader; Ossie's own validator)


def _port_columns(product: str) -> set[str] | None:
    """Columns of the output port named ``product`` (from its tagged ODCS contract), or None."""
    for c in REPO_ROOT.glob(f"domains/*/contracts/{product}.odcs.yaml"):
        defn = odcs.load(c)
        cols: set[str] = set()
        for obj in defn.get("schema") or []:
            if "output_port" in (obj.get("tags") or []):
                cols |= {p["name"] for p in obj.get("properties") or [] if p.get("name")}
        if cols:
            return cols
    return None


def _metric_refs(metric: dict) -> set[str]:
    """Columns referenced by a metric or field expression (``<dataset>.<column>`` -> ``column``)."""
    refs: set[str] = set()
    for d in (metric.get("expression") or {}).get("dialects") or []:
        refs |= set(re.findall(r"\b\w+\.(\w+)\b", d.get("expression") or ""))
    return refs


def main() -> int:
    models = sorted(REPO_ROOT.glob("domains/*/semantics/*.semantic_model.yaml"))
    if not models:
        print("Semantic governance OK: no semantic models yet.")
        return 0

    errors: list[str] = []
    for m in models:
        rel = m.relative_to(REPO_ROOT)
        spec = yaml.safe_load(m.read_text(encoding="utf-8")) or {}
        errors += [f"{rel}: {e}" for e in ossie.validate(spec, "core-spec/ossie-schema.json")]
        if not spec.get("name") or not spec.get("datasets"):
            errors.append(f"{rel}: missing top-level 'name' or 'datasets'")
            continue

        governed: set[str] = set()
        for ds in spec.get("datasets") or []:
            product = str(ds.get("source", "")).split(".")[-1]
            cols = _port_columns(product)
            if cols is None:
                errors.append(
                    f"{rel}: dataset '{ds.get('name')}' source '{ds.get('source')}' is not a "
                    f"governed output port (no output-port ODCS contract for '{product}')"
                )
                continue
            governed |= cols
            if not str(ds.get("description") or "").strip():
                errors.append(f"{rel}: dataset '{ds.get('name')}' has no description")
            for f in ds.get("fields") or []:
                described = bool(str(f.get("description") or "").strip())
                if f.get("name") in cols and described:  # one place per fact (ADR-0027)
                    errors.append(
                        f"{rel}: field '{f.get('name')}' is a contract column — remove its "
                        "description; it is the contract's (and its data element's), inherited"
                    )
                elif f.get("name") not in cols and not described:
                    errors.append(f"{rel}: derived field '{f.get('name')}' has no description")
            bad_fields: set[str] = set()
            for f in ds.get("fields") or []:
                if f.get("name") in cols:
                    continue
                refs = _metric_refs(f)  # derived: built only from contract columns
                if not f.get("name") or not refs or refs - cols:
                    bad_fields.add(str(f.get("name")))
            if bad_fields:
                errors.append(
                    f"{rel}: dataset '{ds.get('name')}' has fields neither in the contract nor "
                    "derived only from contract columns: "
                    f"{sorted(bad_fields)}"
                )

        for metric in spec.get("metrics") or []:
            if not str(metric.get("description") or "").strip():
                errors.append(f"{rel}: metric '{metric.get('name')}' has no description")
            bad = _metric_refs(metric) - governed
            if bad:
                errors.append(
                    f"{rel}: metric '{metric.get('name')}' references non-contract columns: "
                    f"{sorted(bad)}"
                )

    if errors:
        print("Semantic governance FAILED:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(
        f"Semantic governance OK: {len(models)} semantic model(s), all on contract columns"
        " and fully described."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
