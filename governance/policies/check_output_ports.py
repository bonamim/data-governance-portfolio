#!/usr/bin/env python3
"""Publishing guardrails: an output port is declared in ODCS, built, and defined.

Publishing is a deliberate act (ADR-0012): a model is an output port only when its
ODCS contract carries the `output_port` tag (which `datacontract dbt sync` then
writes onto the dbt model). This policy enforces the three-way consistency so
nothing is published by accident and nothing is contracted in vain:

  - every output-port contract (tag `output_port`) has a data product definition and
    a dbt model named after it (in some medallion layer);
  - every data product definition corresponds to an output-port contract.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def _domain_of(path: Path) -> str:
    rel = path.relative_to(REPO_ROOT).parts
    return rel[1] if rel[0] == "domains" else "reference"


def contract_ports() -> dict[str, str]:
    """Return {product: domain} for every output-port ODCS contract (tag output_port)."""
    ports: dict[str, str] = {}
    for c in REPO_ROOT.glob("domains/*/contracts/*.odcs.yaml"):  # excludes contracts/landing/
        doc = yaml.safe_load(c.read_text(encoding="utf-8")) or {}
        schema = (doc.get("schema") or [{}])[0]
        if "output_port" in (schema.get("tags") or []):
            ports[schema.get("name", c.stem)] = _domain_of(c)
    return ports


def _model_file(domain: str, product: str) -> Path | None:
    for p in (REPO_ROOT / "domains" / domain / "dbt" / "models").rglob(f"{product}.sql"):
        return p
    return None


def _definitions() -> set[str]:
    return {p.stem for p in REPO_ROOT.glob("domains/*/data_products/*.yaml")}


def main() -> int:
    ports = contract_ports()
    defs = _definitions()
    violations: list[str] = []

    for product, domain in ports.items():
        if product not in defs:
            violations.append(
                f"output-port contract '{product}' ({domain}) has no data product definition"
            )
        if _model_file(domain, product) is None:
            violations.append(f"output-port contract '{product}' ({domain}) has no dbt model")

    for product in defs:
        if product not in ports:
            violations.append(
                f"data product '{product}' has no output-port contract (tag output_port)"
            )

    if violations:
        print("Output port (publishing guardrail) violations:\n")
        for v in violations:
            print(f"  - {v}")
        print(f"\n{len(violations)} violation(s). See docs/adr/0012-medallion-and-output-ports.md")
        return 1
    print(f"Output ports OK: {len(ports)} published port(s), each contracted, built and defined.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
