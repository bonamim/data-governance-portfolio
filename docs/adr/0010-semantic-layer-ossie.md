# ADR-0010: Apache Ossie as the semantic layer standard

**Status:** Accepted (adoption scheduled for M6/M7)

## Context
The platform exposes governed data products, but **business semantics** — what a metric
*means* (e.g. "delivered order", "on-time delivery rate", "active seller") — were implicit,
spread across dbt SQL, Superset charts, and whatever an AI agent inferred. That is exactly
the fragmentation a data mesh is supposed to remove: the same term computed three different
ways in three tools is a governance failure, not a reporting detail.

[Apache Ossie](https://github.com/apache/ossie) (incubating) is an industry effort to
standardize how semantic metadata is exchanged across analytics, AI and BI platforms — a
vendor-neutral, JSON/YAML, single source of truth for semantic definitions that any tool can
read and write.

## Decision
Adopt **Apache Ossie as the semantic layer standard**. Semantic definitions (entities,
dimensions, metrics) are authored as Ossie specs, owned by the domains alongside their ODCS
contracts, and consumed by **both** reporting (Superset) and the **AI agent** — so a metric
has one definition, governed in git, not one per tool.

Placement in the architecture:

```
data products (dbt, ODCS)  ->  semantic layer (Apache Ossie specs)  ->  Superset  +  AI agent
```

- **Governance** sets which semantic fields are mandatory and that terms resolve to the
  business glossary (federated computational governance — the same pattern as contracts).
- **Domains** own their Ossie specs in `domains/<domain>/semantics/` (config, not platform
  code — ADR-0008).
- **Platform** validates specs in CI and publishes them to DataHub and to the consumers.

Relationship to the existing stack: ODCS governs the **interface and data quality** of a data
product; Ossie governs its **meaning** (metrics and dimensions on top of it). DataHub remains
the catalog/glossary; Ossie is the machine-readable metric contract that Superset and the AI
agent compute from. They are complementary, not competing.

## Consequences
- One governed definition per metric, shared by BI and AI — no "which number is right?".
- New dependency on an **incubating** standard: pin the spec version, keep specs isolated in
  `domains/<domain>/semantics/`, and treat the adapter to each consumer as platform code that
  can be swapped if the standard changes. The risk is acceptable because the specs are plain
  JSON/YAML the platform owns.
- Scheduled for M6 (reporting) and M7 (governed AI); the roadmap and stack are updated to
  include the semantic layer. Proofs will add a scenario: the same metric yields the same
  number in Superset and via the AI agent.
