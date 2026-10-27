#!/usr/bin/env python3
"""The module boundaries of ADR-0008 hold.

A boundary shows in the import name, by convention:

- a platform capability imports as  ``dgp_platform_<capability>``
- a domain package imports as       ``dgp_domain_<domain>[_<suffix>]``

Rules (a violation fails CI):

1. Nothing under ``platform/`` imports a ``dgp_domain_*`` package: the platform never depends on a
   domain.
2. Nothing under ``domains/<A>/`` imports ``dgp_domain_<B>_*`` for another domain B: domains
   integrate through data products and contracts, never through code.

The check reads source files with ``ast``; it imports nothing it checks.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterator
from pathlib import Path

from _checks import report
from dgp_governance import registry
from dgp_governance.repo import root

SCAN_DIRS = ["platform", "domains"]
SKIP_PARTS = {".venv", "__pycache__", "build", "dist", "target", "dbt_packages", ".git"}
DOMAIN_PREFIX = "dgp_domain_"


def domain_of_import(name: str, domains: list[str]) -> str | None:
    """The domain owning an import name, or None if it is not a known domain's package."""
    if not name.startswith(DOMAIN_PREFIX):
        return None
    rest = name.removeprefix(DOMAIN_PREFIX)
    # Longest first, so "customer_experience" matches before "customer".
    for domain in sorted(domains, key=len, reverse=True):
        if rest == domain or rest.startswith(domain + "_"):
            return domain
    return None


def zone_of_path(path: Path) -> tuple[str, str | None]:
    """Classify a file: (``platform`` | ``domain`` | ``other``, its domain or None)."""
    rel = path.relative_to(root()).parts
    if rel[0] == "platform":
        return "platform", None
    if rel[0] == "domains" and len(rel) > 1:
        return "domain", rel[1]
    return "other", None


def imported_top_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def iter_py_files() -> Iterator[Path]:
    for d in SCAN_DIRS:
        for path in (root() / d).rglob("*.py"):
            if not SKIP_PARTS & set(path.parts):
                yield path


def violations() -> list[str]:
    domains = registry.domain_names()
    out: list[str] = []
    for path in iter_py_files():
        zone, owner = zone_of_path(path)
        rel = path.relative_to(root())
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            out.append(f"{rel}: cannot parse ({exc})")
            continue
        for name in imported_top_names(tree):
            if not name.startswith(DOMAIN_PREFIX):
                continue
            imported = domain_of_import(name, domains)
            if zone == "platform":
                out.append(f"{rel}: platform code must not import domain package '{name}' (rule 1)")
            elif zone == "domain" and imported and imported != owner:
                out.append(
                    f"{rel}: domain '{owner}' must not import domain package '{name}' (rule 2)"
                )
    return sorted(out)


def main() -> int:
    return report(
        violations(),
        name="Module boundaries",
        failed="Module boundary violations (ADR-0008)",
        ok="no forbidden cross-imports (ADR-0008)",
        see="docs/adr/0008-monorepo-module-boundaries.md",
    )


if __name__ == "__main__":
    sys.exit(main())
