#!/usr/bin/env python3
"""Agent registry — the mesh system-of-record for the federated domain agents (ADR-0021).

Agentic-mesh governance expects a single registry of *what agents exist, who owns them, and what
data they can touch*. We **derive** it (never hand-maintain it) from the sources that already own
those facts, so it can't drift:

  * the domain registry (``governance/domains/`` — owner, contact),
  * each domain's Apache Ossie semantic models (the governed products its agent can serve),
  * each domain's data products (what else it owns, governed or not yet).

One agent per domain that has at least one governed product. Each agent is the same governed
semantic server scoped to its domain (``DGP_SEMANTIC_DOMAIN``), reached over MCP.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "platform" / "semantics"))
sys.path.insert(0, str(REPO_ROOT))
import ossie  # noqa: E402

from governance.registry import domains  # noqa: E402


# The warehouse role each domain agent connects as: its own read-only agent_<domain> role, granted
# only that domain's output ports (masked where PII) by platform/access/grant_consumers.py — so
# isolation holds in the warehouse as well as in the agent code (M8). The platform-wide view (no
# domain scope) uses the shared least-privilege analytics_reader.
def consumer_role(domain: str) -> str:
    return f"agent_{domain}"


def _domain_meta(name: str) -> dict:
    return next((d for d in domains() if d.get("name") == name), {})


def _governed_products(domain: str) -> list[str]:
    products: list[str] = []
    for path in ossie.models(domain):
        spec = ossie.load(path)
        for ds in ossie.datasets(spec):
            products.append(ds.get("name"))
    return sorted(p for p in products if p)


def agents() -> list[dict]:
    """Every registered domain agent (system-of-record), derived from the mesh definitions."""
    out: list[dict] = []
    for meta in domains():
        name = meta.get("name")
        governed = _governed_products(name)
        if not governed:
            continue  # a domain becomes an agent once it governs at least one product
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
    import json

    print(json.dumps(agents(), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
