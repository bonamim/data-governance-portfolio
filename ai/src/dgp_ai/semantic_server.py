"""The governed semantic layer, exposed as an MCP server (M7, ADR-0020).

Any MCP-capable agent — Claude, ChatGPT, GitHub Copilot, Snowflake Cortex Agents — can obtain a
business metric **only** through these tools, which compile the domain's governed Apache Ossie
definition into SQL (the same definition Superset uses). There is no free-form-SQL tool, so the
agent cannot invent a metric or read an ungoverned (raw/staging/PII) column. The governance is in
the server, so it holds the same for every client; only each client's wiring differs (see
``ai/README.md``).

Transport is client-agnostic via ``MCP_TRANSPORT``: ``stdio`` (default — local clients like Claude
Code and Copilot/VS Code launch the process) or ``streamable-http`` (a remote server for hosted
clients like ChatGPT connectors and Cortex Agents; set ``MCP_HOST``/``MCP_PORT``). Run with
``make ai-mcp`` (stdio) or ``make ai-mcp-http`` (remote); ``DGP_SEMANTIC_DOMAIN`` scopes it to one
domain, which makes it that domain's agent (``make ai-agent DOMAIN=sales``).
"""

from __future__ import annotations

import os

from dgp_governance.repo import root
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from .semantic import SemanticLayer

TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio")
HOST = os.environ.get("MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("MCP_PORT", "8000"))
# A remote client reaches the server through a proxy or tunnel under another hostname, which the
# SDK's DNS-rebinding protection rejects unless it is named here (comma-separated hostnames).
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]

# How to answer, sent to every client at connect time (ChatGPT, Copilot, Cortex as well as Claude):
# numbers only from query_metric, refusals routed to the owner, never around the controls.
INSTRUCTIONS = (root() / "ai" / "analyst-prompt.md").read_text(encoding="utf-8")

layer = SemanticLayer.from_env()
mcp = MCPServer(layer.agent, instructions=INSTRUCTIONS)


@mcp.tool()
def list_data_products() -> list[dict]:
    """List EVERY data product in the mesh, each flagged ``governed`` (has metrics) or not, with its
    owning domain, owner contact, each column's sensitivity (``column_classification``: restricted,
    confidential, internal, public), the privacy category of personal-data columns
    (``column_privacy``: identifier, quasi_identifier, sensitive) and its
    ontology concept (``column_concepts``: columns of different products with the same concept mean
    the same thing, whatever their name). A governed
    product also lists its metrics and dimensions, so call ``query_metric`` next — no need for
    ``list_metrics``. Discovery is open: ungoverned products are visible so a user can ask the
    owner (``request_semantic_model``) to define metrics. Only governed ones are queryable."""
    return layer.list_data_products()


@mcp.tool()
def list_metrics(data_product: str) -> dict:
    """List one data product's governed metrics and dimensions (Ossie spec) with the metric
    expressions. ``list_data_products`` already includes names and descriptions; use this only when
    you need the expressions. For an ungoverned product it names the owner and the next step."""
    return layer.list_metrics(data_product)


@mcp.tool()
def query_metric(
    data_product: str, metrics: list[str], dimensions: list[str] | None = None
) -> dict:
    """Compute governed metric(s) for a data product, optionally broken down by dimension(s).

    ``metrics`` and ``dimensions`` must be names from ``list_data_products`` — anything else is
    refused as "not in the semantic layer". Returns the value rows, the exact governed SQL, the
    Ossie definitions used, the owner, and the classification of every column the number came from
    (so sensitivity needs no extra lookup).
    """
    return layer.query_metric(data_product, metrics, dimensions or [])


@mcp.tool()
def request_semantic_model(data_product: str, business_need: str, requested_by: str = "") -> dict:
    """Ask the owning domain to define governed metrics for a product that isn't governed yet.

    Use this when ``list_data_products`` shows a product with ``governed: false`` and the user wants
    metrics on it. It records an auditable request routed to the owner (not a fabricated number) —
    the self-serve path from discovery to a governed metric. Returns where it was filed and to whom.
    """
    return layer.request_semantic_model(data_product, business_need, requested_by)


def main() -> None:
    if TRANSPORT == "stdio":
        mcp.run(transport="stdio")
        return
    if TRANSPORT != "streamable-http":
        raise SystemExit(f"MCP_TRANSPORT must be stdio or streamable-http, not {TRANSPORT!r}")
    security = None
    if ALLOWED_HOSTS:
        security = TransportSecuritySettings(
            allowed_hosts=["127.0.0.1:*", "localhost:*", *ALLOWED_HOSTS],
            allowed_origins=[
                "http://127.0.0.1:*",
                "http://localhost:*",
                *(f"https://{h}" for h in ALLOWED_HOSTS),
            ],
        )
    mcp.run(transport="streamable-http", host=HOST, port=PORT, transport_security=security)


if __name__ == "__main__":
    main()
