#!/usr/bin/env python3
"""Generate the SQLMesh port models' schema and audits from their ODCS contracts (ADR-0026).

The contract is the source of truth on both engines: dbt gets its tests from
``platform/transform/sync_contracts.py``; here each port model is rewritten from the same contract.

``columns (...)`` — the contract's physical types, for materialized models (FULL / INCREMENTAL).
SQLMesh creates those tables from types it *infers* from the SQL (``varchar`` for text, ``double``
for a rounded numeric), not from the query's real types as ``CREATE TABLE AS`` does; declaring them
makes the contract the schema on this engine too. Views keep the query's own types.

``audits (...)`` — from the same rules:

  required: true          -> not_null(columns := (...))
  unique: true            -> unique_values(columns := (...))
  minimum / maximum       -> accepted_range(column := c, min_v := …, max_v := …)
  rowCount mustBeGreaterThan N -> number_of_rows(threshold := N)   (fails at <= N rows)

An incremental model's audits run on the intervals just built, so a table-level row floor would
judge one month against the whole table's promise: it is left out there (and noted), the same gap
M9's audit view closes on the dbt side.

Run: ``make sqlmesh-sync`` (write) · ``--check`` (drift guard, in ``make check``).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
# port model file -> its ODCS contract
PORTS = {
    "models/customer/customers.sql": "domains/customer/contracts/customers.odcs.yaml",
    "models/customer_experience/reviews.sql": (
        "domains/customer_experience/contracts/reviews.odcs.yaml"
    ),
    "models/sales/orders.sql": "domains/sales/contracts/orders.odcs.yaml",
    "models/sales/sales_performance.sql": "domains/sales/contracts/sales_performance.odcs.yaml",
}
BLOCK = re.compile(r"(?ms)^  audits \(\n.*?^  \)\n")
COLUMNS = re.compile(r"(?ms)^  columns \(\n.*?^  \),\n")
KIND_END = re.compile(r"(?ms)^  kind [A-Z_]+(?: \(\n.*?^  \))?,\n")


def columns(contract: Path) -> list[str]:
    doc = yaml.safe_load(contract.read_text(encoding="utf-8")) or {}
    props = (doc.get("schema") or [{}])[0].get("properties") or []
    return [f"{p['name']} {str(p.get('physicalType', 'text')).upper()}" for p in props]


def audits(contract: Path, incremental: bool) -> list[str]:
    doc = yaml.safe_load(contract.read_text(encoding="utf-8")) or {}
    schema = (doc.get("schema") or [{}])[0]
    props = schema.get("properties") or []
    out = []
    if required := [p["name"] for p in props if p.get("required")]:
        out.append(f"not_null(columns := ({', '.join(required)}))")
    for p in props:
        if p.get("unique"):
            out.append(f"unique_values(columns := ({p['name']}))")
        opts = p.get("logicalTypeOptions") or {}
        if "minimum" in opts or "maximum" in opts:
            bounds = [f"min_v := {opts['minimum']}"] * ("minimum" in opts) + [
                f"max_v := {opts['maximum']}"
            ] * ("maximum" in opts)
            out.append(f"accepted_range(column := {p['name']}, {', '.join(bounds)})")
    for rule in schema.get("quality") or []:
        if rule.get("metric") == "rowCount" and "mustBeGreaterThan" in rule and not incremental:
            out.append(f"number_of_rows(threshold := {rule['mustBeGreaterThan']})")
    return out


def render(model: Path, contract: Path) -> str:
    text = COLUMNS.sub("", model.read_text(encoding="utf-8"))
    incremental = "INCREMENTAL_BY_TIME_RANGE" in text
    if "kind VIEW" not in text:
        cols = ",\n".join(f"    {c}" for c in columns(contract))
        kind = KIND_END.search(text)
        if kind is None:
            raise SystemExit(f"{model}: no `kind` to place the columns after")
        text = text[: kind.end()] + f"  columns (\n{cols}\n  ),\n" + text[kind.end() :]
    lines = ",\n".join(f"    {a}" for a in audits(contract, incremental))
    source = f"    -- generated from {contract.relative_to(REPO)} (sync_contracts.py)\n"
    block = f"  audits (\n{source}{lines}\n  )\n"
    if not BLOCK.search(text):
        raise SystemExit(f"{model}: no `audits (...)` block to rewrite")
    return BLOCK.sub(block, text, count=1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the audits drift")
    args = parser.parse_args()
    drift = []
    for rel, contract in PORTS.items():
        model = HERE / rel
        wanted = render(model, REPO / contract)
        if model.read_text(encoding="utf-8") != wanted:
            drift.append(rel)
            if not args.check:
                model.write_text(wanted, encoding="utf-8")
    if args.check and drift:
        print("SQLMesh port models drifted from the contracts (run `make sqlmesh-sync`):")
        for d in drift:
            print(f"  - portability/sqlmesh/{d}")
        return 1
    print(f"SQLMesh contracts {'OK' if args.check else 'synced'}: {len(PORTS)} port models.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
