#!/usr/bin/env python3
"""Federated governance policy: enforce the module boundaries of ADR-0008.

Boundaries are encoded in import names by convention:
  - platform capability packages import as   dgp_platform_<capability>
  - domain packages import as                dgp_domain_<domain>[_<suffix>]

Rules (a violation fails CI):
  1. platform/** must not import any dgp_domain_* package
     (the platform must never depend on a domain).
  2. domains/<A>/** must not import dgp_domain_<B>_* for any other domain B
     (domains integrate only through data products and contracts, never code).

This is pure-stdlib and path-based, so it runs before anything is installed.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
from governance.registry import domain_names  # noqa: E402

DOMAINS = domain_names()  # single source of truth: governance/domains/
# Longest first so "customer_experience" matches before "customer".
DOMAINS_BY_LEN = sorted(DOMAINS, key=len, reverse=True)

SCAN_DIRS = ["platform", "domains"]
SKIP_PARTS = {".venv", "__pycache__", "build", "dist", "target", "dbt_packages", ".git"}


def domain_of_import(name: str) -> str | None:
    """Return the domain owning an import name, or None if not a domain package."""
    if not name.startswith("dgp_domain_"):
        return None
    rest = name[len("dgp_domain_") :]
    for d in DOMAINS_BY_LEN:
        if rest == d or rest.startswith(d + "_"):
            return d
    return None  # dgp_domain_* with an unknown domain — reported separately


def zone_of_path(path: Path) -> tuple[str, str | None]:
    """Classify a file: ('platform'|'domain'|'other', domain_name|None)."""
    rel = path.relative_to(REPO_ROOT).parts
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


def iter_py_files():
    for d in SCAN_DIRS:
        base = REPO_ROOT / d
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            if SKIP_PARTS & set(path.parts):
                continue
            yield path


def main() -> int:
    violations: list[str] = []
    for path in iter_py_files():
        zone, owner = zone_of_path(path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:  # pragma: no cover - surfaced as a violation
            violations.append(f"{path}: cannot parse ({exc})")
            continue
        rel = path.relative_to(REPO_ROOT)
        for name in imported_top_names(tree):
            imported_domain = domain_of_import(name)
            if imported_domain is None and not name.startswith("dgp_domain_"):
                continue
            if zone == "platform":
                violations.append(
                    f"{rel}: {zone} code must not import domain package '{name}' (ADR-0008 rule 1)"
                )
            elif zone == "domain" and imported_domain and imported_domain != owner:
                violations.append(
                    f"{rel}: domain '{owner}' must not import domain package "
                    f"'{name}' (ADR-0008 rule 2)"
                )

    if violations:
        print("Module boundary violations (ADR-0008):\n")
        for v in sorted(violations):
            print(f"  - {v}")
        print(f"\n{len(violations)} violation(s). See docs/adr/0008-monorepo-module-boundaries.md")
        return 1
    print("Module boundaries OK (ADR-0008): no forbidden cross-imports found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
