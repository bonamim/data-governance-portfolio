"""Single source of truth for the mesh domain registry and the business glossary.

The domain list + metadata live in ``governance/domains/`` and the glossary in
``governance/glossary/`` — read from here by the governance policy checks, the transform
scripts, and the catalog generator, so nothing hardcodes the domain list or the glossary
(which previously drifted across five files).
"""

from __future__ import annotations

from pathlib import Path

import yaml

_GOV = Path(__file__).resolve().parent  # governance/


def domains() -> list[dict]:
    """Every registered mesh domain, as its registry dict (name, owner, description, …)."""
    out = []
    for p in sorted((_GOV / "domains").glob("*.yaml")):
        out.append(yaml.safe_load(p.read_text(encoding="utf-8")) or {})
    return out


def domain_names() -> list[str]:
    return [d["name"] for d in domains()]


def glossary_terms() -> list[dict]:
    """Flattened glossary terms across governance/glossary/*.yaml."""
    out = []
    for p in sorted((_GOV / "glossary").glob("*.yaml")):
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        out.extend(doc.get("terms") or [])
    return out


def classification_terms() -> dict[str, dict]:
    """ODCS classification value -> glossary term dict (id, name, definition)."""
    return {t["classification"]: t for t in glossary_terms() if t.get("classification")}
