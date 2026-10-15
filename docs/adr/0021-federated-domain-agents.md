# ADR-0021: Federated domain agents and a catalog-routed orchestrator

**Status:** Accepted (M7)

## Context
ADR-0020 put *one* platform-hosted governed semantic server over all domains' Ossie specs. That
proves the governance thesis but is centralized: a single agent owns every domain's semantics. A data
mesh pushes ownership to domains — and the AI layer should be no exception. The emerging **"agentic
mesh"** pattern says it directly: AI agents, like data products, should be domain-owned,
discoverable, governed, accountable, and recorded in a registry, with every action auditable.

The question this ADR settles: how do we make the AI layer a mesh — domain-owned agents plus a way to
answer a user's question without them knowing which domain owns which metric — **without** weakening
the governance boundary or making routing a black box.

## Decision

**1. One governed agent per domain (federated data plane).** A domain agent is the *same* governed
semantic server (`ai/semantic-mcp`) scoped to one domain via `DGP_SEMANTIC_DOMAIN`. It then sees and
answers **only** that domain's governed products — another domain's product is invisible, not merely
refused (domain isolation). This is the **self-serve platform** pattern: domains don't write agent
code, they run an instance; the governance (Ossie→SQL, consumer role, no free-form SQL) is identical
for every agent because it is the same code.

**2. A deterministic, catalog-driven orchestrator (router, not supervisor).** `ai/orchestrator`
routes a question to the owning domain by **looking the product up in the catalog** — a *router*
pattern (deterministic dispatch), not an LLM *supervisor* (which picks steps by inference). Routing
decisions are therefore testable and cannot drift or hallucinate. The orchestrator is exposed as one
MCP gateway for clients that want a single endpoint; a client may equally connect to the per-domain
agents directly. Both are the same governed boundary.

**3. An agent registry (system-of-record), derived.** `ai/orchestrator/registry.py` lists each
domain agent — owner, contact, the governed products it serves, its consumer role — **generated**
from the domain registry + the Ossie specs, so it can't drift from reality.

**3a. A governance standard + CI check.** Agents are governed like every other mesh concern: the
standard [`governance/standards/ai-agent.md`](../../governance/standards/ai-agent.md) states the
rules and `governance/policies/check_ai_agents.py` (`make check-ai-agents`, in CI) enforces the
machine-checkable ones — each agent is scoped to a registered domain, serves only its own domain's
output ports (isolation), declares an owner, and exposes no column classified `pii`. This is what
makes "federated **computational** governance" for AI real rather than a claim.

**4. Every agent action is audited.** Each governed call emits a structured log line (timestamp,
agent/domain, tool, allowed|refused, the exact SQL) to stderr and optionally `DGP_AUDIT_LOG` — the
agentic-mesh "reasoning/action trace" that makes data access by AI accountable.

```
   user / LLM client ─▶  orchestrator (router): product → owning domain (catalog) → dispatch
            ┌──────────────┬───────────────┬──────────────┐
            ▼              ▼                ▼
      sales agent    customer agent     cx agent        ← one governed agent per domain
            └─ same boundary: ossie.py → SQL, consumer role, no free SQL, audited ─┘
```

## Why not LangChain / LangGraph in the core
We evaluated them explicitly. Best practice (2026) is: **router** for deterministic dispatch by clear
category; **supervisor** (LangGraph's strength) for *open-ended* work where a model chooses the next
step. Our routing key is deterministic — the catalog already says which domain owns a metric — so a
supervisor would add non-determinism, an LLM dependency, and an API key into the governance-critical,
CI-tested path for no governance benefit. We therefore keep the **governed core framework-free and
deterministic**. LangGraph's supervisor is the right tool for the *optional conversational layer*
(multi-turn NL, clarify, compose across domains) that sits **above** the orchestrator and only ever
calls these governed tools — never routing around them. Tracked as an optional future layer, not
built, so the portfolio stays vendor-neutral and provable without an API key.

## Consequences
- **Mesh-aligned AI**: domain ownership (per-domain agents), self-serve platform (one codebase,
  per-domain instances), federated computational governance (identical boundary everywhere),
  discoverability (registry + catalog routing), accountability (audit log).
- **Proven**: **P11** (`make prove-p11`) asserts isolation (an agent answers only its domain),
  deterministic routing (each product → its owning domain), and that a routed answer equals the direct
  Ossie computation — with P10, AI-via-orchestrator == Superset. Isolation + routing need no
  warehouse; the value check reuses the live platform.
- **PoC vs deployment**: in deployment each agent is its own process (its own `DGP_SEMANTIC_DOMAIN`
  and, in prod, its own least-privilege consumer role/URI) reached over MCP; the in-process router
  scopes the shared module per call so the proof exercises the exact governed path without spawning
  N processes. Snowflake expresses the same shape natively: each domain publishes a semantic view +
  Cortex Agent to the marketplace, and the orchestrator routes across them.
- **M8 outcome**: each domain agent connects as its own read-only `agent_<domain>` role, granted
  only its domain's output ports (masked where PII) via `SEMANTIC_WAREHOUSE_URI_<DOMAIN>`. P11 asserts
  the warehouse denies it every other domain's port, so isolation no longer rests on the agent code
  alone. The platform-wide view uses `analytics_reader`. OAuth + TLS on the remote (`streamable-http`)
  transport and shipping the audit log to a sink are desired production controls (production-readiness
  §5, D1/D2/D7).
