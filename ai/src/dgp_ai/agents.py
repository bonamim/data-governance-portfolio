"""The agent registry: which domain agents exist, who owns them, what they serve (ADR-0021).

Agentic-mesh governance expects one registry of *what agents exist, who owns them and what data
they can touch*. It is derived, never kept by hand, from the sources that already own those facts,
so it can't drift:

* the domain registry (``governance/domains/``): owner and contact;
* each domain's Apache Ossie semantic models: the governed products its agent can serve.

A domain gets an agent once it governs at least one product. Each agent is the same governed
semantic layer scoped to its domain (``DGP_SEMANTIC_DOMAIN``), reached over MCP.

Run: ``make ai-agents`` (prints the registry as JSON).
"""

from __future__ import annotations

import json

from dgp_governance import registry
from dgp_platform_semantics import ossie


def consumer_role(domain: str) -> str:
    """The warehouse role a domain's agent connects as: ``agent_<domain>``, read-only and granted
    only that domain's output ports, masked where PII (``dgp_platform_access.apply_grants``). So
    isolation holds in the warehouse as well as in the agent's code (M8)."""
    return f"agent_{domain}"


def governed_products(domain: str) -> list[str]:
    """The products a domain's semantic models govern (their dataset names)."""
    names = {
        dataset.get("name")
        for path in ossie.models(domain)
        for dataset in ossie.datasets(ossie.load(path))
    }
    return sorted(n for n in names if n)


def agents() -> list[dict]:
    """Every registered domain agent, derived from the mesh's definitions."""
    out: list[dict] = []
    for meta in registry.domains():
        name = meta.get("name")
        governed = governed_products(name)
        if not governed:
            continue
        owner = meta.get("owner") or {}
        out.append(
            {
                "agent": f"dgp-semantic-{name}",
                "domain": name,
                "owner_team": owner.get("team", name),
                "owner_contact": owner.get("contact"),
                "serves": governed,
                "consumer_role": consumer_role(name),
                "scope_env": {"DGP_SEMANTIC_DOMAIN": name},
            }
        )
    return out


def agent_for_domain(domain: str) -> dict | None:
    return next((a for a in agents() if a["domain"] == domain), None)


def main() -> int:
    print(json.dumps(agents(), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
