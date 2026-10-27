#!/usr/bin/env python3
"""Data product definitions meet the standard (governance/standards/data-product.md).

A valid product has a purpose, a type and an owner, a named consumer with a use case, output ports
backed by valid ODCS contracts, inputs that name other output ports, and, for an aggregate, at
least three consuming teams.
"""

from __future__ import annotations

import re
import sys

from _checks import lint_contract, report
from dgp_governance import checks, products, registry
from dgp_governance.repo import relative, root

TYPES = {"source-aligned", "aggregate", "consumer-aligned"}
INPUT = re.compile(r"^[a-z_]+\.[a-z_]+$")  # <domain>.<product>


def _field_violations(product: products.DataProduct, domains: set[str]) -> list[str]:
    """Purpose, type, owner, consumers and inputs: what the definition itself must say."""
    defn, where = product.definition, relative(product.path)
    errs: list[str] = []
    purpose = defn.get("purpose", "")
    if not isinstance(purpose, str) or len(purpose.split()) < 3:
        errs.append(f"{where}: 'purpose' must be a one-sentence verb + object")
    kind = defn.get("type")
    if kind not in TYPES:
        errs.append(f"{where}: 'type' must be one of {sorted(TYPES)} (got {kind!r})")
    if product.owner not in domains:
        errs.append(f"{where}: 'owner.team' must be a known domain (got {product.owner!r})")
    with_use_case = [c for c in defn.get("consumers") or [] if (c or {}).get("use_case")]
    if not with_use_case:
        errs.append(f"{where}: needs a consumer with a use_case (no use case, no product)")
    if kind == "aggregate" and len(with_use_case) < 3:
        errs.append(f"{where}: aggregate needs >= 3 consuming teams (has {len(with_use_case)})")
    errs += [
        f"{where}: input '{ref}' must be a <domain>.<product> output port"
        for ref in product.inputs
        if not INPUT.match(ref)
    ]
    return errs


def _port_violations(product: products.DataProduct) -> list[str]:
    """Every declared output port names a contract that exists and is valid ODCS."""
    where = relative(product.path)
    errs: list[str] = []
    for port in product.definition.get("output_ports") or []:
        contract = (port or {}).get("contract")
        if not contract:
            errs.append(f"{where}: an output_port is missing 'contract'")
        elif not (root() / contract).exists():
            errs.append(f"{where}: output_port contract not found: {contract}")
        else:
            valid, detail = lint_contract(root() / contract)
            if not valid:
                errs.append(f"{contract}: invalid ODCS contract\n      {detail}")
    return errs


def product_violations(product: products.DataProduct, domains: set[str]) -> list[str]:
    """What one data product definition gets wrong."""
    return _field_violations(product, domains) + _port_violations(product)


def main() -> int:
    domains = set(registry.domain_names())
    all_products = products.data_products()
    return report(
        [v for p in all_products for v in product_violations(p, domains)],
        name="Data products",
        failed="Data product governance violations",
        ok=f"{checks.plural(len(all_products), 'product')}, all valid",
        see="governance/standards/data-product.md",
    )


if __name__ == "__main__":
    sys.exit(main())
