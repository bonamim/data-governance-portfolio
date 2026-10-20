#!/usr/bin/env python3
"""AI-agent governance check (ADR-0020, ADR-0021; standard: governance/standards/ai-agent.md).

Applies federated computational governance to the mesh's AI layer. There is one governed agent per
domain (the platform's semantic server scoped to that domain), derived from what the domain already
owns. This check validates every such agent against the standard's CI-checkable rules:

  1. each semantic model sits under a registered domain;
  2. an agent serves ONLY its own domain's output ports (domain isolation);
  3. the agent's domain declares an owner (team + contact) — accountability;
  4. no raw PII is exposed — no field/metric references a column classified ``pii`` in that port's
     ODCS contract (unmasking is governed by ADR-0013, enforced in M8).

Reads only governance + domain definitions (no platform/ or ai/ import), so it is an INDEPENDENT
check and respects the module boundaries (ADR-0008). Behavioural rules (governed-tools-only, least
privilege, audit, deterministic routing) are properties of the shared boundary, proven by P10/P11.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
from governance.registry import classification_terms, domains  # noqa: E402


def _pii_values() -> set[str]:
    """ODCS ``classification`` values that mean PII, from the glossary (not hardcoded)."""
    out = set()
    for value, term in classification_terms().items():
        if "PII" in {str(term.get("name", "")).upper(), str(term.get("id", "")).upper()}:
            out.add(value)
    return out


def _product_owner(product: str) -> str | None:
    """owner.team of the data product named ``product`` (its definition), or None."""
    for path in REPO_ROOT.glob(f"domains/*/data_products/{product}.yaml"):
        defn = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return (defn.get("owner") or {}).get("team")
    return None


def _pii_columns(product: str, pii_values: set[str]) -> set[str]:
    """PII-classified columns of the output port named ``product``, from its ODCS contract."""
    cols: set[str] = set()
    for c in REPO_ROOT.glob(f"domains/*/contracts/{product}.odcs.yaml"):
        defn = yaml.safe_load(c.read_text(encoding="utf-8")) or {}
        for obj in defn.get("schema") or []:
            for prop in obj.get("properties") or []:
                if prop.get("name") and prop.get("classification") in pii_values:
                    cols.add(prop["name"])
    return cols


def _metric_refs(metric: dict) -> set[str]:
    refs: set[str] = set()
    for d in (metric.get("expression") or {}).get("dialects") or []:
        refs |= set(re.findall(r"\b\w+\.(\w+)\b", d.get("expression") or ""))
    return refs


def main() -> int:
    models = sorted(REPO_ROOT.glob("domains/*/semantics/*.semantic_model.yaml"))
    if not models:
        print("AI-agent governance OK: no agents yet (no semantic models).")
        return 0

    registry = {d["name"]: d for d in domains()}
    pii_values = _pii_values()
    errors: list[str] = []
    agents: set[str] = set()

    for m in models:
        rel = m.relative_to(REPO_ROOT)
        domain = m.relative_to(REPO_ROOT / "domains").parts[0]
        agents.add(domain)

        # 1. registered domain
        meta = registry.get(domain)
        if meta is None:
            errors.append(f"{rel}: domain '{domain}' is not in the registry")
            continue
        # 3. accountable owner
        owner = meta.get("owner") or {}
        if not owner.get("team") or not owner.get("contact"):
            errors.append(f"agent '{domain}': domain registry missing owner.team or owner.contact")

        spec = yaml.safe_load(m.read_text(encoding="utf-8")) or {}
        for ds in spec.get("datasets") or []:
            product = str(ds.get("source", "")).split(".")[-1]
            # 2. domain isolation — the served product is owned by THIS agent's domain
            port_owner = _product_owner(product)
            if port_owner is None:
                errors.append(f"agent '{domain}': product '{product}' has no data-product def")
            elif port_owner != domain:
                errors.append(
                    f"agent '{domain}': serves '{product}' owned by '{port_owner}' (isolation)"
                )
            # 4. no raw PII exposed
            pii = _pii_columns(product, pii_values)
            if pii:
                exposed = {f.get("name") for f in ds.get("fields") or []} & pii
                for field in ds.get("fields") or []:  # a derived field must not wrap PII either
                    exposed |= _metric_refs(field) & pii
                for metric in spec.get("metrics") or []:
                    exposed |= _metric_refs(metric) & pii
                if exposed:
                    errors.append(
                        f"agent '{domain}': exposes PII column(s) {sorted(exposed)} of '{product}' "
                        f"— must be masked (ADR-0013) or excluded"
                    )

    if errors:
        print("AI-agent governance FAILED:")
        for e in errors:
            print(f"  - {e}")
        return 1
    names = ", ".join(sorted(agents))
    print(f"AI-agent governance OK: {len(agents)} agent(s) ({names}), all valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
