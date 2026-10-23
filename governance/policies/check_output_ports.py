#!/usr/bin/env python3
"""Publishing guardrails: an output port is declared in ODCS, built, defined and documented.

Publishing is a deliberate act (ADR-0012): a model is an output port only when its
ODCS contract carries the `output_port` tag (which `datacontract dbt sync` then
writes onto the dbt model). This policy enforces the three-way consistency so
nothing is published by accident and nothing is contracted in vain:

  - every output-port contract (tag `output_port`) has a data product definition and
    a dbt model named after it (in some medallion layer);
  - every data product definition corresponds to an output-port contract;
  - every output-port contract is complete (governance/standards/data-product.md): the
    table has a description, and every column has a description and a classification.
    A port is an interface consumers (people, BI and AI agents) read without asking
    the owner, so a blank column is a promise nobody made.
"""

from __future__ import annotations

import sys
from pathlib import Path

from dgp_platform_quality import odcs

REPO_ROOT = Path(__file__).resolve().parents[2]


def _domain_of(path: Path) -> str:
    rel = path.relative_to(REPO_ROOT).parts
    return rel[1]


def _port_contracts() -> dict[str, tuple[str, Path, dict]]:
    """Return {product: (domain, path, schema object)} for every output-port ODCS contract."""
    ports: dict[str, tuple[str, Path, dict]] = {}
    for c in REPO_ROOT.glob("domains/*/contracts/*.odcs.yaml"):  # excludes contracts/landing/
        doc = odcs.load(c)
        schema = (doc.get("schema") or [{}])[0]
        if "output_port" in (schema.get("tags") or []):
            ports[schema.get("name", c.stem)] = (_domain_of(c), c, schema)
    return ports


def _is_described(text: object, name: str) -> bool:
    """A real description: non-blank and not just the column name restated."""
    t = str(text or "").strip()
    return bool(t) and t.lower().replace(" ", "_") != name.lower()


def completeness_violations(schema: dict, where: str) -> list[str]:
    """Missing documentation on one output-port schema object (table + columns)."""
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


def _model_file(domain: str, product: str) -> Path | None:
    for p in (REPO_ROOT / "domains" / domain / "dbt" / "models").rglob(f"{product}.sql"):
        return p
    return None


def _definitions() -> set[str]:
    return {p.stem for p in REPO_ROOT.glob("domains/*/data_products/*.yaml")}


def main() -> int:
    ports = _port_contracts()
    defs = _definitions()
    violations: list[str] = []

    for product, (domain, path, schema) in ports.items():
        if product not in defs:
            violations.append(
                f"output-port contract '{product}' ({domain}) has no data product definition"
            )
        if _model_file(domain, product) is None:
            violations.append(f"output-port contract '{product}' ({domain}) has no dbt model")
        violations += completeness_violations(schema, str(path.relative_to(REPO_ROOT)))

    for product in defs:
        if product not in ports:
            violations.append(
                f"data product '{product}' has no output-port contract (tag output_port)"
            )

    if violations:
        print("Output port (publishing guardrail) violations:\n")
        for v in violations:
            print(f"  - {v}")
        print(
            f"\n{len(violations)} violation(s). See docs/adr/0012-medallion-and-output-ports.md"
            " and governance/standards/data-product.md (output-port completeness)."
        )
        return 1
    print(
        f"Output ports OK: {len(ports)} published port(s), each contracted, built, defined"
        " and fully documented."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
