# `ai/`: Governed AI (M7)

This is the mesh's AI consumer. It answers business questions by computing metrics from the **same
Apache Ossie definitions Superset uses** (ADR-0010), so BI and AI give the *same number* rather
than two guesses. `ai/` is a consumer application, a peer of `reporting/` (ADR-0017), not platform
code.

```
ai/
  src/dgp_ai/
    semantic.py              the governed engine: a SemanticLayer, platform-wide or scoped to one domain
    semantic_server.py       one SemanticLayer as an MCP server
    agents.py                the agent registry, derived from the domains and their semantic models
    router.py                routes each question to the owning domain's agent
    orchestrator_server.py   the router as one MCP endpoint
  tests/
  eval/                      the same questions asked with and without the governed tools, graded
  analyst-prompt.md          how the assistant should answer (sent to every client as instructions)
  analyst.settings.json      the locked-down Claude Code session for analysts (make ai-chat)
  analyst.mcp.json           the MCP servers that session may use
  .mcp.json                  a template for wiring your own client
```

## Two planes: open discovery, governed computation

This isn't a locked box. It separates *finding out what exists* from *trusting a number*:

- **Discovery is open.** `list_data_products` returns every data product in the mesh, says whether
  each one is governed (has a semantic model), and names its owning domain. The `datahub` server
  adds schemas, lineage and classifications on top.
- **Computation is governed.** `query_metric` serves only metrics defined in an Ossie model. They
  are compiled to SQL by the shared reader (`dgp_platform_semantics.ossie`, the same one behind
  the Superset publisher) and run as a **consumer role**. There is no free-form SQL, so raw,
  staging and PII columns can't be asked for, which is exactly what keeps BI and AI on the same
  number.
- **A bridge, so it's never a dead end.** Want a metric on a product that isn't governed yet?
  `request_semantic_model` files an auditable request with the owning domain
  ([`governance/requests/`](../governance/requests/README.md)) and tells the owner. The domain
  writes the Ossie model, and then the metric can be queried.

## The AI layer is a mesh too (ADR-0021)

Ownership is federated for AI as well. There is one governed agent **per domain**, and a router
sends each question to the domain that owns the product:

- **Domain agents.** A `SemanticLayer` scoped to one domain (`DGP_SEMANTIC_DOMAIN`). `make ai-agent
  DOMAIN=sales` runs the Sales agent, which sees and answers only Sales' governed products. It is
  the same code and the same boundary everywhere: the platform ships it, each domain runs one.
- **Agent registry** (`make ai-agents`). The system of record: each agent, its owner, the products
  it serves and the warehouse role it uses. It is derived from the domain registry and the Ossie
  models, so it can't drift.
- **Router** (`make ai-orchestrator`). One MCP endpoint that looks up the owning domain in the
  catalog and hands the question to its agent. It is a **deterministic router**, not an LLM
  supervisor, so it is testable (P11) and can't hallucinate which domain answers.
- **Audit log.** Every governed call logs `{ts, agent, tool, allowed|refused, sql}` to stderr (and
  to `DGP_AUDIT_LOG` if set), so AI data access is accountable.

```
   user / LLM client ─▶ router: product → owning domain (catalog) → that domain's agent
            ┌──────────────┬───────────────┬──────────────┐
            ▼              ▼                ▼
      sales agent    customer agent     cx agent     ← one governed agent per domain
            └─ the same boundary: Ossie → SQL, consumer role, no free SQL, audited ─┘
```

A client can talk to the router (one endpoint) or to a domain's agent directly. Both are the same
governed boundary. In production each agent runs as its own process with its own least-privilege
role (`agent_<domain>`). The Snowflake-native equivalent is each domain publishing a semantic view
and a Cortex Agent, with the router choosing between them.

### Why not LangChain or LangGraph in the core

Routing here is deterministic: the catalog already says which domain owns a metric. So a router is
the right pattern, and the governed core stays framework-free and provable (no LLM and no API key
in CI). A LangGraph supervisor is the right tool for an *optional* conversational layer (several
turns, clarifying questions, combining domains) that sits above the router and only calls the
governed tools. It is a documented option, not built, so the core stays vendor-neutral.

## How it's governed

The governance lives in the **server**, so it holds the same way for every client (Claude,
ChatGPT, Copilot, Cortex). Only the wiring differs. There are two MCP servers:

| Server | Role | Source |
|---|---|---|
| `datahub` (the official `mcp-server-datahub`) | **Discovery**: the product, its owner, PII classification, lineage | DataHub (M4) |
| `semantic` (`dgp_ai.semantic_server`, ours) | **The governed number**: compute a metric, or discover or request one where none exists | the domains' Ossie models |

```
                    ┌── datahub MCP  → "sales.sales_performance, owned by Sales, revenue is public"
   any MCP client ──┤
                    └── semantic MCP → query_metric → Ossie reader → SQL → warehouse (consumer role)
                                                        ▲
                         Superset (M6) ────────────────┘  the same reader, the same definition
```

## Tools

- `list_data_products()`: every product, whether it is governed, and its owner (open discovery).
- `list_metrics(data_product)`: the governed metrics and dimensions, or, for an ungoverned product,
  who owns it and how to request a model.
- `query_metric(data_product, metrics, dimensions=[])`: the value, plus the exact governed SQL and
  the Ossie definitions used, so every answer traces back to its one definition.
- `request_semantic_model(data_product, business_need, requested_by)`: ask for an ungoverned
  product to be governed. The request goes to the owning domain and is announced on its alert
  webhook (`ALERT_WEBHOOK_<DOMAIN>`), and the answer says whether the owner was actually notified.

## Works with any MCP client

MCP is the cross-vendor standard: Claude, **ChatGPT** (remote MCP connectors), **GitHub Copilot**
(VS Code agent mode) and **Snowflake Cortex Agents** all speak it. The server offers two transports
through `MCP_TRANSPORT`:

- **`stdio`** (the default, `make ai-mcp`): the client starts the process locally. Use it for
  **Claude Desktop and Code** and **Copilot in VS Code**.
- **`streamable-http`** (`make ai-mcp-http`, on `MCP_HOST:MCP_PORT`): a **remote** server, for
  hosted clients such as **ChatGPT connectors** and **Cortex Agents**. Anywhere beyond localhost,
  put it behind TLS and authentication.

Wiring per client (all of them reach the *same* server and tools):

| Client | Transport | How to wire it |
|---|---|---|
| **Claude Code** | stdio | copy [`ai/.mcp.json`](.mcp.json) to the repo root, or use `claude mcp add` |
| **Claude Desktop** | stdio | merge the `.mcp.json` entries into `claude_desktop_config.json` (absolute paths) |
| **GitHub Copilot** (VS Code) | stdio | add the same `command` and `args` to `.vscode/mcp.json` (`servers:` block) |
| **ChatGPT** | streamable-http | `make ai-mcp-http` behind a public HTTPS URL, added as a custom connector ([below](#test-it-from-chatgpt)) |
| **Snowflake Cortex Agents** | streamable-http | register the server's URL in the agent's tool configuration |

The server starts with `uv run --directory <repo> --package dgp-ai --no-dev python -m
dgp_ai.semantic_server`, so the client needs nothing installed but `uv`.

> **Cortex's native alternative.** Cortex Analyst also has its own semantic model (Snowflake
> semantic views). Because Ossie is our source of truth, the same define-once pattern used for
> Superset applies: generate the semantic view from the Ossie model. That deeper integration is in
> the backlog. The MCP path above works today on any warehouse.

## Run it

1. Bring the platform up and publish a port to Superset, so the agreement proof has a BI side:
   `make up && make bi-up && make bi-publish`. For discovery, start DataHub: `make catalog-up`.
2. **DataHub token** for the `datahub` server: **none needed locally.** The local GMS runs with
   `METADATA_SERVICE_AUTH_ENABLED=false` and serves tokenless calls as the system user.
   `.mcp.json` keeps a placeholder, `_PROD_DATAHUB_GMS_TOKEN`, which the server ignores. **In
   production** authentication is on (docs/production-readiness.md): create a service-account
   token (DataHub → Settings → Access Tokens) and rename the key to `DATAHUB_GMS_TOKEN`.
3. Edit [`.mcp.json`](.mcp.json): the repository path, and the **consumer-role** warehouse URI
   (`SEMANTIC_WAREHOUSE_URI`, as `analytics_reader`). A domain's agent connects as its own
   `agent_<domain>` role through `SEMANTIC_WAREHOUSE_URI_<DOMAIN>` (the Make targets set both).
   Postgres accepts TLS only, so add
   `?sslmode=verify-full&sslrootcert=<repo>/platform/core/infra/tls/out/ca.crt`.
4. Start the server, `make ai-mcp` (stdio) or `make ai-mcp-http` (remote), and wire your client from
   the table above.

### Analysts in Claude Code: `make ai-chat`

A plain Claude Code session in this repository has a shell and can read `.env` (owner passwords)
and `data/` (the raw Olist CSVs). It could go around the semantic layer, leaving the refusal to the
model's judgment. `make ai-chat` starts a session whose only path to data is the two MCP servers:

- [`analyst.settings.json`](analyst.settings.json) denies the shell (`Bash`, `Monitor`), edits,
  subagents and web fetches, and reading `.env*`, `.mcp.json`, `data/`, CSV and Parquet files and
  TLS keys. It pre-approves the `semantic` and `datahub` tools.
- [`analyst.mcp.json`](analyst.mcp.json) holds no secrets. The launcher runs `env -i` and passes
  only the repository path and the `analytics_reader` URI, so no other credential reaches the
  session.
- [`analyst-prompt.md`](analyst-prompt.md) sets how it answers: every number from `query_metric`
  with its SQL and caveats, sensitivity from DataHub, and a refusal routed to the owning domain
  (`request_semantic_model`) rather than around the controls. That is guidance. The deny rules are
  the enforcement.
- DataHub ingestion never profiles with sample values, so the `datahub` server can't hand out raw
  column values either.

No special prompt wording is needed. "List the customer_unique_id values" is refused by
`query_metric` (it isn't in the semantic layer), and Postgres masking (ADR-0013) still applies to
the consumer role underneath. `make prove-p21` checks all of this. Pass flags through with `ARGS`,
for example `make ai-chat ARGS='-p "total revenue by state"'`. Keep using plain `claude` for
platform development.

## Test it from ChatGPT

ChatGPT runs in OpenAI's cloud: it can't start a local process or reach `localhost`, so it needs the
streamable-HTTP server on a public HTTPS URL. It has no shell or files on this machine either, so the
server is the whole boundary, and there is no `make ai-chat`-style lockdown to apply. The guidance in
[`analyst-prompt.md`](analyst-prompt.md) reaches it as the server's MCP `instructions`.

1. Tunnel a public URL to the port: `ngrok http 8900` (or `cloudflared tunnel --url
   http://127.0.0.1:8900`). Note the hostname, for example `abc123.ngrok-free.app`.
2. Start the server and name that host (the SDK's DNS-rebinding protection rejects any other `Host`
   header with a `421`): `MCP_ALLOWED_HOSTS=abc123.ngrok-free.app make ai-mcp-http`.
3. In ChatGPT, turn on developer mode for connectors (Settings → Apps & Connectors → Advanced) and
   create a connector with the URL `https://abc123.ngrok-free.app/mcp` and no authentication, then
   enable it in a new chat. Menu names change between ChatGPT releases, and developer mode needs a
   plan that offers it.
4. Ask the questions below, and compare the answers with `make prove-p10`.

Only the `semantic` server is exposed this way: `mcp-server-datahub` speaks stdio and SSE, and the
local GMS runs without authentication, so keep DataHub off the public URL.

> **The URL has no authentication.** Anyone who has it can query the governed metrics and file
> requests into `governance/requests/`. Use it for a short test only, and stop the tunnel after. A
> real deployment puts the server behind OAuth 2.1 (docs/production-readiness.md).

## Examples

> **Ask:** "What's total revenue by state, and is it sensitive?"
>
> 1. `datahub` finds `sales.sales_performance`: owned by Sales, `total_revenue` classified public.
> 2. `semantic.query_metric("sales_performance", ["total_revenue"], ["customer_state"])` returns the
>    rows by state, plus the governed SQL: the same metric Superset charts.

> **Explore, then request:** "How many distinct customers are there per state?"
>
> `list_data_products` shows `customers` as not governed (owned by Customer). `query_metric`
> refuses, since there is no governed metric yet, and points to the owner.
> `request_semantic_model("customers", "...")` files the request with Customer. Once Customer adds
> the Ossie model, the metric can be queried and agrees with BI.

> **Refusal:** "Show me customers' email addresses." Refused: *"not in the semantic layer."* The
> agent has no path to an ungoverned column, and the warehouse masks PII for its role anyway
> (ADR-0013).

## Proofs

- `make prove-p10`: for every governed metric, the **AI** path, the **BI** path (Superset's
  published metric) and a **direct Ossie** control return the same value.
- `make prove-p11`: the agentic mesh: a domain's agent answers only its own products, the router
  sends each product to its owning domain every time, and a routed metric equals the direct Ossie
  number, so federation changes nothing a consumer sees.
- `make prove-p21`: the analyst session. Only allow-listed environment, no shell, no secrets, only
  the governed servers, and every way of weakening it is caught.

All three are deterministic (no LLM and no API key), except the optional live session in P21.

## Portability

`SEMANTIC_WAREHOUSE_URI` is the only engine-specific setting, and the metric SQL is ANSI (ADR-0009). To
move to Snowflake, point the URI at Snowflake. Nothing in `dgp_ai` or the Ossie models changes (as
in [`platform/context/semantics/README.md`](../platform/context/semantics/README.md)).
