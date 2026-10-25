#!/usr/bin/env python3
"""Catalog-driven router — the governable orchestrator core (ADR-0021).

A user's question shouldn't require knowing which domain owns which metric. The router answers that
**deterministically from the catalog** (a *router* pattern, not an LLM *supervisor*): it finds the
product's owning domain, then dispatches to that domain's governed agent. Deterministic dispatch is
what makes the orchestration testable (proof P11) and governable — the routing decision can't drift
or hallucinate.

The agents are the same governed semantic code scoped per domain (``semantic.DOMAIN``). In
deployment each is its own process (``DGP_SEMANTIC_DOMAIN``) over MCP; here the router scopes the
shared module per call so routing + isolation exercise the **exact** governed path — no second
boundary implementation. The LLM/NL layer (a client, optionally a LangGraph supervisor) sits *above*
this and only ever calls these governed tools; it never routes around them.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "ai" / "semantic_mcp"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import registry  # noqa: E402
import semantic  # noqa: E402  (the one governed boundary — ossie->SQL, consumer role, no free SQL)


@contextlib.contextmanager
def _scoped(domain: str | None):
    """Run the governed agent as ``domain`` (mirrors a per-domain process scoped by env)."""
    prev = semantic.DOMAIN
    semantic.DOMAIN = domain
    try:
        yield
    finally:
        semantic.DOMAIN = prev


def list_agents() -> list[dict]:
    """The registered domain agents (system-of-record)."""
    return registry.agents()


def catalog() -> list[dict]:
    """Every data product across all domains (platform-wide discovery)."""
    with _scoped(None):
        return semantic.list_data_products()


def owning_domain(product: str) -> str | None:
    """Which domain owns ``product`` — the deterministic routing key, from the catalog."""
    return next((p["domain"] for p in catalog() if p["data_product"] == product), None)


def route_query(
    product: str, metrics: list[str], dimensions: list[str] | None = None, requested_by: str = ""
) -> dict:
    """Route a metric question to the owning domain's governed agent and return its answer."""
    domain = owning_domain(product)
    if domain is None:
        raise ValueError(f"'{product}' is not a data product in the mesh (see list_data_products).")
    semantic._audit(
        "route", "dispatched", product=product, metrics=metrics, requested_by=requested_by or None
    )
    with _scoped(domain):
        result = semantic.query_metric(product, metrics, dimensions or [])
    agent = registry.agent_for_domain(domain)
    result["routed_to"] = agent["agent"] if agent else f"dgp-semantic-{domain}"
    result["domain"] = domain
    return result


def route_request(product: str, business_need: str, requested_by: str = "") -> dict:
    """Route a 'please govern this product' request to its owning domain."""
    domain = owning_domain(product)
    if domain is None:
        raise ValueError(f"'{product}' is not a data product in the mesh (see list_data_products).")
    with _scoped(domain):
        return semantic.request_semantic_model(product, business_need, requested_by)
