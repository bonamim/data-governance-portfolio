#!/usr/bin/env python3
"""Every domain agent meets the AI-agent standard (governance/standards/ai-agent.md).

Federated computational governance, applied to the mesh's AI layer (ADR-0020, ADR-0021). Each
domain that governs a product has one agent (the platform's semantic layer scoped to that
domain), derived from what the domain already owns. This check holds every such agent to the
standard's CI-checkable rules:

1. each semantic model sits under a registered domain.
2. an agent serves ONLY its own domain's output ports (domain isolation).
3. the agent's domain names an owner, team and contact (accountability).
4. no raw personal data is exposed: no field or metric reads a column of the port whose privacy
   category (governed tag ``privacy_category``, SNOWFLAKE.CORE.PRIVACY_CATEGORY) is
   ``identifier`` or ``sensitive``. Unmasking is governed by ADR-0013. A quasi-identifier, such as
   a customer's state, may be a dimension.

It reads the governed definitions only and imports none of the agent's code (``dgp_ai``), so it
checks the agents independently of how they are built. Their runtime rules (governed tools only,
least privilege, audit, deterministic routing) are properties of the shared boundary, proven by
P10 and P11.
"""

from __future__ import annotations

import sys
from pathlib import Path

from _checks import report
from dgp_governance import checks, products, registry
from dgp_governance.repo import domain_of, relative
from dgp_platform_semantics import ossie

# Privacy categories an agent never exposes raw: data that identifies a person on its own, or is
# sensitive about one. Quasi-identifiers (zip, city, state) are fine as aggregate dimensions.
PERSONAL = {"identifier", "sensitive"}


def personal_columns(product: str) -> set[str]:
    """The output port's columns holding personal data an agent must not expose."""
    port = products.output_port(product)
    if port is None:
        return set()
    return {
        c["name"]
        for c in port.columns
        if registry.column_tags(c).get("privacy_category") in PERSONAL
    }


def exposed_columns(spec: dict, dataset: dict, personal: set[str]) -> set[str]:
    """The personal columns a dataset's fields, or the model's metrics, would hand out."""
    exposed = {f.get("name") for f in ossie.fields(dataset)} & personal
    for field in ossie.fields(dataset):  # a derived field must not wrap PII either
        exposed |= ossie.referenced_columns(field) & personal
    for metric in ossie.metrics(spec):
        exposed |= ossie.referenced_columns(metric) & personal
    return exposed


def model_violations(path: Path, owners: dict[str, dict]) -> list[str]:
    """What the agent serving this semantic model gets wrong."""
    domain = domain_of(path)
    meta = owners.get(domain)
    if meta is None:  # 1. a registered domain
        return [f"{relative(path)}: domain '{domain}' is not in the registry"]
    errs = []
    owner = meta.get("owner") or {}
    if not owner.get("team") or not owner.get("contact"):  # 3. an accountable owner
        errs.append(f"agent '{domain}': domain registry missing owner.team or owner.contact")

    spec = ossie.load(path)
    for dataset in ossie.datasets(spec):
        product = ossie.source_table(dataset)
        definition = products.data_product(product)
        port_owner = definition.owner if definition else None
        if definition is None:  # 2. domain isolation: it serves its own domain's products
            errs.append(f"agent '{domain}': product '{product}' has no data-product definition")
        elif port_owner != domain:
            errs.append(f"agent '{domain}': serves '{product}' owned by '{port_owner}' (isolation)")
        if exposed := exposed_columns(spec, dataset, personal_columns(product)):  # 4. no raw PII
            errs.append(
                f"agent '{domain}': exposes PII column(s) {sorted(exposed)} of '{product}', which "
                "must be masked (ADR-0013) or excluded"
            )
    return errs


def main() -> int:
    models = ossie.models()
    owners = {d["name"]: d for d in registry.domains()}
    agents = sorted({domain_of(m) for m in models})
    return report(
        [v for m in models for v in model_violations(m, owners)],
        name="AI agents",
        failed="AI-agent governance FAILED",
        ok=f"{checks.plural(len(agents), 'agent')} ({', '.join(agents)}), all valid",
    )


if __name__ == "__main__":
    sys.exit(main())
