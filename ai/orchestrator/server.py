#!/usr/bin/env python3
"""The orchestrator, exposed as one MCP server (ADR-0021).

A single governed endpoint for a client that doesn't want to wire up every domain agent itself: it
discovers products across all domains, lists the domain agents, and **routes** each metric question
to the owning domain's agent (deterministically, via the catalog). A client may instead connect to
the per-domain agents directly (``ai/semantic_mcp`` scoped by ``DGP_SEMANTIC_DOMAIN``) — both are
the same governed boundary. Transport is client-agnostic (``MCP_TRANSPORT``: stdio|streamable-http).

Run: ``make ai-orchestrator``.
"""

from __future__ import annotations

import os

import router
from mcp.server.mcpserver import MCPServer

TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio")
mcp = MCPServer("dgp-orchestrator")


@mcp.tool()
def list_agents() -> list[dict]:
    """List the federated domain agents (who owns each, and which governed products it serves)."""
    return router.list_agents()


@mcp.tool()
def list_data_products() -> list[dict]:
    """List EVERY data product across all domains, flagged governed/ungoverned with its owner."""
    return router.catalog()


@mcp.tool()
def query_metric(
    data_product: str, metrics: list[str], dimensions: list[str] | None = None
) -> dict:
    """Answer a governed metric question by routing it to the owning domain's agent.

    Returns the value rows, governed SQL, Ossie definitions, and which domain agent answered.
    ``metrics``/``dimensions`` must be governed names (from ``list_metrics`` on it) — else it is
    refused. Routing is deterministic (by catalog), so the same question always reaches the
    same domain.
    """
    return router.route_query(data_product, metrics, dimensions or [])


@mcp.tool()
def request_semantic_model(data_product: str, business_need: str, requested_by: str = "") -> dict:
    """Route a 'please define governed metrics for this product' request to its owning domain."""
    return router.route_request(data_product, business_need, requested_by)


if __name__ == "__main__":
    mcp.run(transport=TRANSPORT)
