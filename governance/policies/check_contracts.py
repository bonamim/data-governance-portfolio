#!/usr/bin/env python3
"""Every dataset the platform loads into raw has a valid ODCS contract of what its source holds.

The raw layer is a governed boundary (docs/data-mesh.md): many data products consume it, so each
dataset declares an ODCS contract and that contract must be valid. A landing file's lives in
``contracts/landing/``, an operational database table's in ``contracts/oltp/``. This fails CI when
a dataset has no contract, or its contract doesn't pass ``datacontract lint``.

For a landing dataset it also checks that the contract's operational rules are ones the loader can
run (ADR-0024): the partition scheme and source, the file-name pattern, the retention class and the
completion signal. An OLTP table has none: the loader copies it whole, and refuses it when its
columns no longer match the contract.

Every contract is documented like an output port: the table has a description, and every column a
description and a classification, its own or inherited from its definition (ADR-0027). The raw
table is what every consuming domain reads first, so it can't be the one place a column goes
unexplained. And no column uses a reserved prefix (``_system_``, ``_internal_``, ADR-0028): a source
contract describes what the source holds, and those names belong to the platform and the data
products, which add them after.

Output-port contracts are covered by ``check_output_ports.py`` and ``check_data_products.py``.
"""

from __future__ import annotations

import re
import sys

from _checks import completeness_violations, lint_contract, report
from dgp_governance import checks, registry
from dgp_governance.repo import relative, root
from dgp_platform_contracts import odcs
from dgp_platform_ingestion.config import load_datasets
from dgp_platform_ingestion.layout import LandingRules
from dgp_platform_quality.rules import load_contract, source_contract_path


def source_datasets() -> list[tuple[str, str, str]]:
    """(domain, dataset, source type) for every dataset the platform loads into raw: delivered
    through the landing zone, or copied from an operational database."""
    return [(d.domain, d.name, d.source_type) for d in load_datasets(root())]


def violations() -> list[str]:
    out: list[str] = []
    for domain, dataset, source_type in source_datasets():
        path = source_contract_path(root(), domain, dataset, source_type)
        where = relative(path)
        if not path.exists():
            out.append(f"{domain}.{dataset}: no ODCS contract at {where}")
            continue
        valid, detail = lint_contract(path)
        if not valid:
            out.append(f"{where}: ODCS contract is invalid\n      {detail}")
        if source_type == "landing":
            try:
                problems = LandingRules.from_contract(load_contract(path)).problems()
            except (re.error, ValueError) as exc:  # e.g. a filenamePattern that isn't valid regex
                problems = [str(exc)]
            out += [f"{where}: landing rule: {p}" for p in problems]
        reserved = registry.reserved_prefixes()
        for schema in odcs.load(path).get("schema") or []:
            out += completeness_violations(schema, where)
            out += [
                f"{where}: column '{p['name']}' uses a reserved prefix ({', '.join(reserved)}): "
                "the platform and the data products add those after the gate"
                for p in schema.get("properties") or []
                if str(p.get("name", "")).startswith(reserved)
            ]
    return out


def main() -> int:
    return report(
        violations(),
        name="Source contracts",
        failed="Contract governance violations",
        ok=f"{checks.plural(len(source_datasets()), 'source dataset')} (landing and OLTP), "
        "valid and documented",
        see="docs/data-mesh.md (governed boundaries)",
    )


if __name__ == "__main__":
    sys.exit(main())
