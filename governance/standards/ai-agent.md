# Standard: what makes a valid AI agent

Portable governance standard (the **WHAT**, above the governance boundary) for the mesh's AI layer
(ADR-0020 governed AI, ADR-0021 federated domain agents). It applies federated **computational**
governance to AI: the same "govern once, enforce everywhere, check in CI" pattern used for data
products. It is not prose guidance but an enforced contract. The platform enforces the machine-checkable
parts in CI (`governance/policies/check_ai_agents.py`), and the rest is review guidance.

## 1. An agent is a platform capability bound to a domain, not hand-written

There is **one governed agent per domain** (ADR-0021). An agent is not a bespoke artefact a domain
authors. It is the platform's governed semantic server (`dgp_ai.semantic_server`) **scoped to one domain**
(`DGP_SEMANTIC_DOMAIN`). A domain therefore "has an agent" exactly when it owns ≥ 1 governed semantic
model. The agent is **derived** from what the domain already owns (its Ossie specs + output ports +
registry entry), so it cannot drift and no agent config is hand-maintained (self-serve). The
**agent registry** (`dgp_ai.agents`, `make ai-agents`) is the system-of-record:
agent id, owning domain, owner contact, the products it serves, its consumer role.

This is why there is no `domains/<domain>/agents/` manifest: declaring the agent separately would
duplicate the semantics/products the domain already governs and would violate self-serve. The agent
is governed by **this standard + its check + the registry**, not by a second hand-edited file.

## 2. Required (CI-checkable)

| Rule | Check |
|---|---|
| Every semantic model (an agent's capability) sits under a **registered domain** | `governance/policies/check_ai_agents.py` |
| An agent serves **only its own domain's** output ports (domain isolation): each served product's data-product `owner.team` equals the agent's domain | same |
| The agent's domain declares `owner.team` **and** `owner.contact` (accountability: who owns this agent) | same |
| **No raw PII is exposed**: no field or metric in a domain's semantic model references a column classified `pii` in that port's ODCS contract (unmasking is governed by ADR-0013, enforced in M8) | same |
| Served columns resolve to governed output-port columns (an agent can't invent data) | `check_semantics.py` (ADR-0010) |

## 3. Enforced by the governed boundary (ADR-0020/0021), proven by P10/P11

These hold for every agent because they are properties of the shared platform code, not per-domain
config. They are proven by the proofs rather than re-checked per file:

- **Governed tools only:** the sole way to a metric is `query_metric`, which compiles the Ossie
  definition to SQL. There is no free-form-SQL tool, so ungoverned / raw / PII columns are
  unanswerable (ADR-0020).
- **Least privilege:** an agent connects as a **consumer role** (output ports only), never a
  superuser or an owner role. Per-domain least-privilege roles land in M8.
- **Audited:** every call is logged `{ts, agent, tool, allowed|refused, sql}` (agentic-mesh action
  trace).
- **Deterministic routing:** the orchestrator routes by the **catalog** (a router, not an LLM
  supervisor). Any natural-language layer sits *above* the governed tools and never routes around
  them. Proven by **P11**, and metric agreement with BI proven by **P10**.

## 4. Ownership

The **domain** owns its agent (its semantics, its output ports, accountability). The **platform**
owns the agent runtime and the governed boundary. **Governance** owns this standard and the glossary
the PII rule reads. One accountable owner per agent and many consumers, the same split as data products
([data-product.md](data-product.md), ADR-0011).
