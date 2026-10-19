# ADR-0020: Governed AI through MCP (semantic computation + catalog discovery)

**Status:** Accepted (M7)

## Context
ADR-0010 named two consumers of the Apache Ossie semantic layer: reporting (Superset, delivered in
M6) and an **AI agent**. An agent asked "what is total revenue by state?" will, left to itself,
write its own SQL — the "same term, three tools, three numbers" failure the mesh exists to prevent.
We need AI to compute from the **same governed definition** as BI, and to be *bound* by the mesh's
governance (grants, classification), not merely encouraged to respect it.

The agent is an MCP client (Claude Desktop / Claude Code). The question is what it is allowed to call.

## Decision
Expose the mesh to the agent through **two MCP servers**, splitting *discovery* from *computation*:

- **DataHub MCP** (official `acryl` `mcp-server-datahub`) — discovery and governance: find the data
  product, its owner, PII/Internal/Public classification, and lineage. Read-only here.
- **A governed semantic MCP server** (`ai/semantic-mcp`, ours) — the **only** way to obtain a metric
  value. Its `query_metric` tool compiles the domain's Ossie definition into SQL via the shared
  `platform/semantics/ossie.py` reader — the *same* translation the Superset adapter uses — and runs
  it against the warehouse as the **consumer role** (`marketplace_role`, output ports only).

There is deliberately **no free-form-SQL tool**. A name that is not a governed metric or dimension in
the spec is refused ("not in the semantic layer"), so raw/staging/PII columns are unanswerable by
construction.

**Two planes, so governance isn't a cage.** Discovery is open — `list_data_products` returns *every*
data product with a `governed` flag and its owner — while *computation* is governed. When a user
wants a metric on an ungoverned product, `request_semantic_model` files an auditable request to the
owning domain (`governance/requests/`, mirroring `governance/approvals/`); the domain
authors the Ossie spec and it becomes queryable. Exploration is never a dead end, and no ungoverned
number is ever fabricated.

**Client-agnostic.** The governance lives in the server, so it holds for any MCP client — Claude,
ChatGPT, GitHub Copilot, Snowflake Cortex Agents (MCP is the 2026 cross-vendor standard). Only the
wiring differs. The server exposes `stdio` (local clients launch it) and `streamable-http` (a remote
server for hosted clients) via `MCP_TRANSPORT`; `ai/.mcp.json` wires Claude, and `ai/README.md`
tabulates the rest. No bespoke agent code or API key.

```
                    ┌── datahub MCP   (discover: port, owner, PII class, lineage)
   any MCP client ──┤
                    └── semantic MCP  (query_metric → ossie.py → SQL → warehouse, consumer role)
                                              ▲
                           Superset (M6) ─────┘  same ossie.py, same definition
```

## Consequences
- **One definition, enforced in code.** BI and AI translate Ossie→SQL through the same `ossie.py`, so
  there is no second parser to drift. Proof **P10** (`make prove-p10`) asserts, per metric, that the
  AI path, Superset's published metric, and a direct-Ossie control return the same number —
  deterministic, no LLM.
- **Governance holds for AI.** The agent inherits consumer-role grants (output ports only) and cannot
  reach ungoverned columns; it tightens automatically when ADR-0013 dynamic masking lands in M8.
- **Portable.** The metric SQL is ANSI (ADR-0009); only `SEMANTIC_WAREHOUSE_URI` changes for a
  different warehouse (e.g. Snowflake), exactly as for the Superset adapter.
- **Dependencies.** DataHub MCP needs a DataHub PAT only where GMS auth is on (production,
  `METADATA_SERVICE_AUTH_ENABLED=true`); the local stack runs with auth off and serves it tokenless.
  Neither is required for the semantic server or P10. The
  semantic server follows the existing adapter-script convention (a `uv run --no-project` script, not
  a workspace package), so it adds no lock churn.
- **Not adopted:** a standalone SDK agent / NL loop (Claude-as-client is leaner and keeps CI free of
  an API key); a runtime semantic service (Cube-style) — rejected in ADR-0010. When Superset's native
  semantic-layer connection (SIP-182) GAs, the same Ossie specs feed it (see the README backlog).
- **Federated domain agents + orchestration — see ADR-0021.** This ADR's single platform-hosted
  server is now also deployable as **one agent per domain** with a catalog-routing orchestrator (the
  agentic-mesh shape, proof P11). The governance boundary here is unchanged; ADR-0021 only adds
  per-domain scoping, routing, a derived agent registry, and an audit log.
