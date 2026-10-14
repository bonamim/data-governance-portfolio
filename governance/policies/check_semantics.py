#!/usr/bin/env python3
"""Semantic-layer governance check (ADR-0010, milestone M6).

Every Apache Ossie semantic model (``domains/*/semantics/*.semantic_model.yaml``) must be built only
on **governed output-port columns**: each dataset's ``source`` resolves to an output port (its ODCS
contract), and every field and metric references a column that exists in that contract. So the
business semantics that Superset and the AI agent compute can't drift from the data contract.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def _port_columns(product: str) -> set[str] | None:
    """Columns of the output port named ``product`` (from its tagged ODCS contract), or None."""
    for c in REPO_ROOT.glob(f"domains/*/contracts/{product}.odcs.yaml"):
        defn = yaml.safe_load(c.read_text(encoding="utf-8")) or {}
        cols: set[str] = set()
        for obj in defn.get("schema") or []:
            if "output_port" in (obj.get("tags") or []):
                cols |= {p["name"] for p in obj.get("properties") or [] if p.get("name")}
        if cols:
            return cols
    return None


def _metric_refs(metric: dict) -> set[str]:
    """Column names referenced by a metric expression (``<dataset>.<column>`` -> ``column``)."""
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
            bad_fields = {f.get("name") for f in ds.get("fields") or []} - cols - {None}
            if bad_fields:
                errors.append(
                    f"{rel}: dataset '{ds.get('name')}' has fields not in the contract: "
                    f"{sorted(bad_fields)}"
                )

        for metric in spec.get("metrics") or []:
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
    print(f"Semantic governance OK: {len(models)} semantic model(s), all on contract columns.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
