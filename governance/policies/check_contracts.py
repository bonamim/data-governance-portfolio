#!/usr/bin/env python3
"""Every landing dataset has a valid ODCS contract, with rules the loader can execute.

The landing zone is a governed boundary (docs/data-mesh.md): many data products consume its files,
so each landing dataset declares an ODCS contract and that contract must be valid. This fails CI
when a landing dataset has no contract, or its contract doesn't pass ``datacontract lint``.

It also checks that the contract's operational rules are ones the loader can run (ADR-0024): the
partition scheme and source, the file-name pattern, the retention class and the completion signal.

And that the contract is documented like an output port: the file has a description, and every
column a description and a classification, its own or inherited from its definition (ADR-0027).
The landing file is what every consuming domain reads first, so it can't be the one place a column
goes unexplained.

Output-port contracts are covered by ``check_output_ports.py`` and ``check_data_products.py``.
"""

from __future__ import annotations

import re
import sys

from _checks import completeness_violations, lint_contract, report
from dgp_governance import checks
from dgp_governance.repo import relative, root
from dgp_platform_contracts import odcs
from dgp_platform_ingestion.config import load_datasets
from dgp_platform_ingestion.layout import LandingRules
from dgp_platform_quality.rules import contract_path, load_contract


def landing_datasets() -> list[tuple[str, str]]:
    """(domain, dataset) for every dataset delivered through the landing zone."""
    return [(d.domain, d.name) for d in load_datasets(root()) if d.source_type == "landing"]


def violations() -> list[str]:
    out: list[str] = []
    for domain, dataset in landing_datasets():
        path = contract_path(root(), domain, dataset)
        where = relative(path)
        if not path.exists():
            out.append(f"{domain}.{dataset}: no ODCS contract at {where}")
            continue
        valid, detail = lint_contract(path)
        if not valid:
            out.append(f"{where}: ODCS contract is invalid\n      {detail}")
        try:
            problems = LandingRules.from_contract(load_contract(path)).problems()
        except (re.error, ValueError) as exc:  # e.g. a filenamePattern that isn't a valid regex
            problems = [str(exc)]
        out += [f"{where}: landing rule: {p}" for p in problems]
        for schema in odcs.load(path).get("schema") or []:
            out += completeness_violations(schema, where)
    return out


def main() -> int:
    return report(
        violations(),
        name="Landing contracts",
        failed="Contract governance violations",
        ok=f"{checks.plural(len(landing_datasets()), 'landing dataset')}, valid and documented",
        see="docs/data-mesh.md (governed boundaries)",
    )


if __name__ == "__main__":
    sys.exit(main())
