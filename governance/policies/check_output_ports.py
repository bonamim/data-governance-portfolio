#!/usr/bin/env python3
"""Publishing guardrails: an output port is declared in ODCS, built, defined and documented.

Publishing is a deliberate act (ADR-0012): a model is an output port only when its ODCS contract
carries the ``output_port`` tag, which ``datacontract dbt sync`` then writes onto the dbt model.
This policy keeps the three in step, so nothing is published by accident and nothing is contracted
in vain:

- every output-port contract has a data product definition, and a dbt model of the same name in
  some medallion layer;
- every data product definition has an output-port contract;
- every output-port contract is complete (governance/standards/data-product.md): the table has a
  description, and every column has a description and a classification. A port is an interface
  that people, BI and AI agents read without asking the owner, so a blank column is a promise
  nobody made.
"""

from __future__ import annotations

import sys

from _checks import report
from dgp_governance import checks, products
from dgp_governance.repo import relative


def _is_described(text: object, name: str) -> bool:
    """A real description: not blank, and not just the name restated."""
    t = str(text or "").strip()
    return bool(t) and t.lower().replace(" ", "_") != name.lower()


def completeness_violations(schema: dict, where: str) -> list[str]:
    """Missing documentation on one output-port schema object (the table and its columns)."""
    out: list[str] = []
    table = schema.get("name", "?")
    if not _is_described(schema.get("description"), table):
        out.append(f"{where}: table '{table}' has no description")
    for prop in schema.get("properties") or []:
        col = prop.get("name", "?")
        if not _is_described(prop.get("description"), col):
            out.append(f"{where}: column '{col}' has no description")
        if not prop.get("classification"):
            out.append(f"{where}: column '{col}' has no classification")
    return out


def violations() -> list[str]:
    ports = products.output_ports()
    defined = {p.name for p in products.data_products()}
    out: list[str] = []
    for name, port in ports.items():
        if name not in defined:
            out.append(
                f"output-port contract '{name}' ({port.domain}) has no data product definition"
            )
        if port.model_file is None:
            out.append(f"output-port contract '{name}' ({port.domain}) has no dbt model")
        out += completeness_violations(port.schema, relative(port.contract))
    out += [
        f"data product '{name}' has no output-port contract (tag output_port)"
        for name in sorted(defined - set(ports))
    ]
    return out


def main() -> int:
    return report(
        violations(),
        name="Output ports",
        failed="Output port (publishing guardrail) violations",
        ok=f"{checks.plural(len(products.output_ports()), 'port')}, each contracted, built, "
        "defined and documented",
        see="docs/adr/0012-medallion-and-output-ports.md and governance/standards/data-product.md "
        "(output-port completeness)",
    )


if __name__ == "__main__":
    sys.exit(main())
