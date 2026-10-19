# `ai/` — Governed AI (M7)

The AI consumer of the mesh. It answers business questions by computing metrics from the **same
Apache Ossie semantic definitions as Superset** (ADR-0010) — so BI and AI return the *same number*,
not two guesses. `ai/` is a consumer-app capability, a peer to `reporting/` (ADR-0017), not platform
code.

## Two planes: open discovery, governed computation

The design is **not** a locked box. It separates *exploring what exists* from *trusting a number*:

- **Discovery is open.** `list_data_products` returns **every** data product in the mesh, flagged
  `governed` (has a semantic model) or not, with its owning domain. Users explore freely — and the
  `datahub` server adds schemas, lineage and classifications on top.
- **Computation is governed.** `query_metric` only serves metrics defined in an Ossie spec, compiled
  to SQL by the shared `platform/semantics/ossie.py` reader (the same one Superset uses) and run as
  the **consumer role**. No free-form SQL, so ungoverned/raw/PII columns are unanswerable — which is
  exactly what keeps BI and AI on the same number.
- **The bridge, so it's never a dead end.** Want a metric on an ungoverned product?
  `request_semantic_model` files an auditable request to the owning domain
  (`governance/requests/`) — the self-serve path from "I found it" to "it's governed",
  mirroring the breaking-change approval flow. The domain authors the Ossie spec; then it's queryable.

## The AI layer is a mesh too (ADR-0021)

Governance isn't the only mesh principle we apply to AI — **ownership** is federated as well. There's
one governed agent **per domain**, and an orchestrator routes each question to the owning domain:

- **Per-domain agents** (`semantic-mcp` scoped by `DGP_SEMANTIC_DOMAIN`) — `make ai-agent DOMAIN=sales`
  runs the Sales agent, which sees and answers **only** Sales' governed products (domain isolation).
  Same code, same governed boundary; the platform ships it, each domain runs an instance (self-serve).
- **Agent registry** (`make ai-agents`) — the system-of-record: each agent, its owner, the products
  it serves, its consumer role. Derived from the domain + Ossie definitions, so it can't drift.
- **Orchestrator** (`ai/orchestrator`, `make ai-orchestrator`) — a single MCP endpoint that looks up
  the owning domain **in the catalog** and dispatches to its agent. Routing is a **deterministic
  router**, not an LLM supervisor — so it's testable (P11) and can't hallucinate which domain answers.
- **Audit log** — every governed call logs `{ts, agent, tool, allowed|refused, sql}` (stderr /
  `DGP_AUDIT_LOG`), so AI data access is accountable.

```
   user / LLM client ─▶ orchestrator (router): product → owning domain (catalog) → dispatch
            ┌──────────────┬───────────────┬──────────────┐
            ▼              ▼                ▼
      sales agent    customer agent     cx agent     ← one governed agent per domain
            └─ same boundary: ossie.py → SQL, consumer role, no free SQL, audited ─┘
```

A client can talk to the **orchestrator** (one endpoint, routed) or to the **per-domain agents**
directly — both are the same governed boundary. In production each agent is its own process (and, with
M8, its own least-privilege role); the Snowflake-native shape is each domain publishing a semantic
view + Cortex Agent to the marketplace, with the orchestrator routing across them.

### Why not LangChain / LangGraph (in the core)
Routing here is **deterministic** — the catalog already says which domain owns a metric — so a
*router* is the right pattern, and the governed core stays framework-free and provable (no LLM, no API
key in CI). LangGraph's **supervisor** is the right tool for an *optional* conversational layer
(multi-turn, clarify, compose across domains) that sits **above** the orchestrator and only calls the
governed tools — never routing around them. It's a documented future option, not built, so the core
stays vendor-neutral.

## How it's governed

The governance lives in the **server**, so it holds identically for every client (Claude, ChatGPT,
Copilot, Cortex); only the wiring differs. Two MCP servers:

| Server | Role | Source |
|---|---|---|
| `datahub` (official acryl `mcp-server-datahub`) | **Discovery / governance** — find the product, owner, PII classification, lineage | DataHub (M4) |
| `semantic` (`semantic-mcp/`, ours) | **The governed number** — compute a metric; discover/request where none exists yet | the domains' Ossie specs |

```
                    ┌── datahub MCP  → "sales.sales_performance, owned by Sales, revenue is PUBLIC"
   any MCP client ──┤
                    └── semantic MCP → query_metric → ossie.py → SQL → warehouse (consumer role)
                                                        ▲
                         Superset (M6) ────────────────┘  same ossie.py, same definition
```

## Tools (`semantic-mcp/server.py`)

- `list_data_products()` — every product, `governed` flag + owner (open discovery).
- `list_metrics(data_product)` — governed metrics + dimensions; for an ungoverned product, explains
  who owns it and how to request a model.
- `query_metric(data_product, metrics, dimensions=[])` — the value **plus** the exact governed SQL
  and the Ossie definitions used, so every answer is traceable to the one definition.
- `request_semantic_model(data_product, business_need, requested_by)` — file a request to govern an
  ungoverned product; routed to the owning domain.

## Works with any MCP client

MCP is the cross-vendor standard (2026): Claude, **ChatGPT** (remote MCP connectors), **GitHub
Copilot** (VS Code agent mode), and **Snowflake Cortex Agents** all speak it. The server exposes two
transports via `MCP_TRANSPORT`:

- **`stdio`** (default, `make ai-mcp`) — the client launches the process locally. Use for **Claude
  Desktop/Code** and **Copilot in VS Code**.
- **`streamable-http`** (`make ai-mcp-http`, listens on `MCP_HOST:MCP_PORT`) — a **remote** MCP
  server. Use for hosted clients that connect over HTTP: **ChatGPT connectors** and **Cortex
  Agents**. Put it behind TLS + auth for anything beyond localhost (hardening lands with M8).

Wiring per client (all point at the *same* server and tools):

| Client | Transport | Wire it |
|---|---|---|
| **Claude Code** | stdio | copy/symlink [`ai/.mcp.json`](.mcp.json) to the repo root, or `claude mcp add` |
| **Claude Desktop** | stdio | merge the `.mcp.json` entries into `claude_desktop_config.json` (absolute paths) |
| **GitHub Copilot** (VS Code) | stdio | add the same `command`/`args` to `.vscode/mcp.json` (`servers:` block) |
| **ChatGPT** | streamable-http | run `make ai-mcp-http`, add it as a **connector**/custom MCP server by URL (`http://host:8900/mcp`) |
| **Snowflake Cortex Agents** | streamable-http | register the remote MCP server URL in the agent's tool config |

> **Cortex, the native alternative.** Cortex Analyst also has its *own* semantic model (Snowflake
> "semantic views"). Because Ossie is our source of truth, the same define-once-project-into-each-tool
> pattern we use for Superset applies: generate a Cortex semantic view from the Ossie spec. That's the
> deeper Snowflake-native integration (tracked in the backlog); the MCP tool path above works today
> on any warehouse.

## Run it

1. Platform + warehouse up and a port published to Superset (so the agreement proof has a BI side):
   `make up && make bi-up && make bi-publish`. DataHub for discovery: `make catalog-up`.
2. **DataHub token** (for the `datahub` server): **none locally.** The local stack runs GMS with
   `METADATA_SERVICE_AUTH_ENABLED=false`, so tokenless calls are served (as the system user) and the
   UI's Access Tokens page shows "Token based authentication is currently disabled". `.mcp.json`
   keeps the token parked as `_PROD_DATAHUB_GMS_TOKEN`, which the server ignores. **In production**
   auth is on (docs/production-readiness.md): generate a service-account PAT (DataHub → Settings →
   Access Tokens) and rename the key to `DATAHUB_GMS_TOKEN`.
3. Edit [`.mcp.json`](.mcp.json): repo path and the **consumer-role** warehouse URI
   (`SEMANTIC_WAREHOUSE_URI`, as `analytics_reader`). A domain-scoped agent connects as its own
   `agent_<domain>` role through `SEMANTIC_WAREHOUSE_URI_<DOMAIN>` (the Make targets set both).
   Postgres is TLS-only, so add `?sslmode=verify-full&sslrootcert=<repo>/platform/infra/tls/out/ca.crt`
   (or at least `sslmode=require`).
4. Start the semantic server: `make ai-mcp` (stdio) or `make ai-mcp-http` (remote), then wire your
   client from the table above.

**Analysts in Claude Code: `make ai-chat`.** A plain Claude Code session in this repo also has a
shell and can read `.env` (owner passwords) and `data/` (raw Olist CSVs), so it can bypass the
semantic layer and leave the refusal to the model's judgment. `make ai-chat` starts a session whose
only data path is the two MCP servers:

- [`analyst.settings.json`](analyst.settings.json) denies the shell (`Bash`, `Monitor`), edits,
  subagents and web fetches, and reads of `.env*`, `.mcp.json`, `data/`, CSV/Parquet files and TLS
  keys; it pre-approves the `semantic` and `datahub` tools.
- [`analyst.mcp.json`](analyst.mcp.json) holds no secrets: the launcher runs `env -i` and passes only
  the repo path and the `analytics_reader` URI, so no other credential reaches the session.
- [`analyst-prompt.md`](analyst-prompt.md) sets how it answers: every number from `query_metric`
  with its SQL and caveats, sensitivity from DataHub, and a refusal routed to the owning domain
  (`request_semantic_model`), never around the controls (another session, a SQL client, a raw file).
  That is guidance; the deny rules are the enforcement.
- DataHub ingestion never profiles with sample values, so the `datahub` server can't hand out raw
  column values either.

No prompt wording needed: "list customer_unique_id values" is refused by `query_metric` (not in the
semantic layer), and Postgres masking (ADR-0013) still applies to the consumer role underneath.
`make prove-p21` checks all of this. Pass flags through with `ARGS`, e.g. `make ai-chat ARGS='-p "total revenue by state"'`. Keep
using plain `claude` for platform development.

## Example (governed Q&A)

> **Ask:** "What's total revenue by state, and is it sensitive?"
>
> 1. `datahub` → finds `sales.sales_performance` (owner: Sales; `total_revenue` classified **Public**).
> 2. `semantic.query_metric("sales_performance", ["total_revenue"], ["customer_state"])` → rows by
>    state, **plus** the governed SQL, the same metric Superset charts.

> **Explore + request (not a dead end):** "Give me order volume by month."
>
> `list_data_products` shows `orders` with `governed: false` (owned by Sales). `query_metric` refuses
> (no governed metric yet) but points to the owner; `request_semantic_model("orders", "...")` files
> the request to Sales. Once Sales adds the Ossie spec, the metric is queryable and BI-consistent.

> **Refusal:** "Show me customers' email addresses." → **refused**: *"not in the semantic layer."*
> The agent has no path to ungoverned columns — governance holds even for AI (and tightens with
> ADR-0013 masking in M8).

## Proofs

- `make prove-p10` — for every governed metric, the **AI** path, the **BI** path (Superset's
  published metric) and a **direct-Ossie** control return the same value: "BI and AI agree", tested.
- `make prove-p11` — the **agentic mesh**: a domain agent answers only its own products (isolation),
  the orchestrator routes each product to its owning domain deterministically, and a routed metric
  equals the direct Ossie number (so federation changes nothing a consumer sees).

Both are deterministic (no LLM / API key).

## Portability

`SEMANTIC_WAREHOUSE_URI` is the only engine-specific knob — the metric SQL is ANSI (ADR-0009). Moving
to Snowflake: point the URI at Snowflake, nothing else in `semantic-mcp/` or the Ossie specs changes
(same story as `platform/semantics/README.md`).
