"""Catalog-driven router — the orchestrator's governable core (ADR-0021).

Someone asking a question shouldn't need to know which domain owns which metric. The router works
that out **deterministically, from the catalog** — a router, not an LLM supervisor: it finds the
product's owning domain and hands the question to that domain's governed agent. Deterministic
dispatch is what makes the orchestration testable (proof P11) and governable: the routing decision
can't drift or hallucinate.

Each agent is the same governed semantic layer scoped to a domain. Deployed, each runs as its own
process (``DGP_SEMANTIC_DOMAIN``) over MCP; here the router holds one ``SemanticLayer`` per domain,
so routing and isolation run through the exact governed path, with no second implementation of the
boundary. An LLM or natural-language layer (a client, optionally a LangGraph supervisor) sits
above this and only ever calls these governed tools; it never routes around them.
"""

from __future__ import annotations

from collections.abc import Sequence

from . import agents
from .semantic import SemanticLayer, audit

ROUTER = "dgp-orchestrator"


class Router:
    """Routes each question to the semantic layer of the domain that owns the product."""

    def __init__(self) -> None:
        self.platform = SemanticLayer()  # the platform-wide view, for discovery

    def agent(self, domain: str) -> SemanticLayer:
        """The governed agent of one domain."""
        return SemanticLayer(domain)

    def list_agents(self) -> list[dict]:
        """The registered domain agents."""
        return agents.agents()

    def catalog(self) -> list[dict]:
        """Every data product across all domains (platform-wide discovery)."""
        return self.platform.list_data_products()

    def owning_domain(self, product: str) -> str | None:
        """Which domain owns ``product`` — the routing key, from the catalog."""
        return next((p["domain"] for p in self.catalog() if p["data_product"] == product), None)

    def route_query(
        self,
        product: str,
        metrics: Sequence[str],
        dimensions: Sequence[str] = (),
        requested_by: str = "",
    ) -> dict:
        """Answer a metric question with the owning domain's agent."""
        domain = self._owner(product)
        audit(
            ROUTER,
            "route",
            "dispatched",
            product=product,
            metrics=list(metrics),
            requested_by=requested_by or None,
        )
        result = self.agent(domain).query_metric(product, metrics, dimensions)
        registered = agents.agent_for_domain(domain)
        result["routed_to"] = registered["agent"] if registered else self.agent(domain).agent
        result["domain"] = domain
        return result

    def route_request(self, product: str, business_need: str, requested_by: str = "") -> dict:
        """Hand a "please govern this product" request to its owning domain."""
        return self.agent(self._owner(product)).request_semantic_model(
            product, business_need, requested_by
        )

    def _owner(self, product: str) -> str:
        domain = self.owning_domain(product)
        if domain is None:
            raise ValueError(
                f"'{product}' is not a data product in the mesh (see list_data_products)."
            )
        return domain
