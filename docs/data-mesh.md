# Data Mesh Design

This document describes how the four data mesh principles are applied, which domain owns
which data, and how domains share data with each other safely.

---

## 1. Domains

The Olist dataset contains nine files. Each one is assigned to exactly one owning domain.

| Domain | Owns (source datasets) | Source system | Data products (output ports) |
|---|---|---|---|
| **Sales** | `orders`, `order_items`, `order_payments` | Postgres `shop_oltp` | `sales.orders`, `sales.sales_performance`, `sales.seller_performance` |
| **Customer** | `customers` | Postgres `shop_oltp` | `customer.customers` |
| **Marketplace** | `products`, `product_category_name_translation`, `sellers` | `shop_oltp` + landing zone (sellers file) | none yet |
| **Customer Experience** | `order_reviews` | Landing zone (reviews files) | `customer_experience.reviews` |
| **Reference** | `geolocation` | Landing zone | none yet |

A domain publishes an output port only for a named consumer with a use case
([data-product standard](../governance/standards/data-product.md)): marketplace and reference ingest
and govern their data (source contracts, raw tables) but publish nothing until someone needs it. A
source contract is not an output port either way. It governs what a source delivers, and consumers
read the owning domain's published port, never the landing file or its raw table.

Each domain is registered in `governance/domains/` with its owner, team, and contact channel.

**Reference** is a domain like the others, in `domains/reference/` with its own team: its data
products are the reference data many domains share (geolocation), and it owns the shared value types
of the ontology that describe places (zip-code prefix, city, state). Being shared does not make data
the platform's: the platform runs the tools, a domain owns the data and its meaning.

### Cross-domain consumption

Domains consume each other's **data products only**, never raw or staging tables.

```mermaid
graph LR
    CUST["customer.customers"] --> PERF["sales.sales_performance"]
    REV["customer_experience.reviews"] --> PERF
    ORD["sales.orders"] --> PERF
    PROD["marketplace.products"] -.-> PERF
    GEO["reference.geolocation"] -.-> CUST
```

Solid arrows are the inputs the data product definitions declare today. Dotted ones are ports that
would follow once a consumer asks for them. These dependencies are what the contract gate protects:
when Customer changes `customer.customers` in a breaking way, Sales is the consumer it names.

---

## 2. Data as a product

A dataset is a **data product** only when it has all of the following. CI rejects a data
product definition that is missing any of them.

| Requirement | Where it lives |
|---|---|
| One-sentence **purpose** (verb + object) and a real **use case** | Data product definition |
| **Type**: source-aligned / aggregate / consumer-aligned | Data product definition |
| Owner (team + accountable person) | Contract `team` section |
| Declared **consumers** | Data product definition |
| ODCS contract with schema and quality rules | `domains/<domain>/contracts/` |
| SLA: freshness and availability | Contract `slaProperties` |
| Classification of every column (public / internal / PII) | Contract column `classification` |
| Semantic version | Contract `version`, with a major bump required for breaking changes |
| Certification status in DataHub | Set automatically when all quality checks pass |

### Data product heuristics

We apply the [data-mesh-architecture data product heuristics](https://www.datamesh-architecture.com/data-product-heuristics).
They define the standard for *what is a valid data product* and are the basis of
`governance/standards/data-product.md` (some are CI-checkable, some are review guidance):

- **Fundamental:** a clearly defined consumer and use case ("without a use case there is no
  data product"), one stable owner, data quality and semantics **consistent across all output
  ports**, low integration burden (consumers never have to stitch products together for a
  simple question), and a bounded, maintainable scope.
- **By type:**
  - *Source-aligned:* semantically coherent (one meaning per concept), a single business
    domain, and a contained source **blast radius** (a source change touches only this product).
  - *Aggregate:* build only when **three or more** teams need the same facts. General-purpose
    content only, with dedicated ownership, and never a mini-warehouse.
  - *Consumer-aligned:* one decision context, boundaries that follow **process** lines not
    system boundaries, a named business consumer, and only what that team needs.

The **smallest useful standalone unit** wins: decompose by domain/process, not by source
system or artifact type.

---

## 3. Self-serve platform

The platform team owns `platform/`. A domain gets these capabilities without writing
infrastructure code:

| Capability | Domain provides | Platform provides |
|---|---|---|
| Ingestion | A source config in `domains/<domain>/ingestion/` | Loaders, scheduling, lineage emission |
| Quality | Quality rules in its contracts | Test runner, quality gate, results in DataHub |
| Transformation | A dbt project | Airflow integration, write-audit-publish, warehouse roles, CI |
| Cataloging | Contracts and data product definitions | DataHub ingestion, ownership and domain assignment |
| Access | List of consumers + PII masking policies in contracts | Postgres roles and grants, generated from definitions. Role-based masking views for PII (ADR-0013), and a least-privilege `analytics_reader` for BI/AI |

Adding a fifth domain means adding a folder under `domains/`. No platform change is needed.

---

## 4. Federated computational governance

Governance rules are agreed centrally by a governance guild (one representative per
domain plus the platform team) and **enforced as code**, not by review meetings.

| Global policy | Enforced by |
|---|---|
| Every dataset has an ODCS contract with an owner | CI contract validation |
| Every column has a classification, and PII columns have a masking rule | CI contract validation |
| Breaking changes need a major version bump and consumer sign-off | CI breaking-change gate |
| Consumers read output ports only | Postgres grants: consumer roles have `SELECT` on output schemas only |
| Data products are certified only when quality checks pass | Quality runner → DataHub certification |
| Shared terms come from the glossary | CI checks glossary references resolve |

Domains stay free to decide everything else: their models, naming inside staging,
release cadence, and tests beyond the global minimum.

### Where "quality" lives

"Quality" is three responsibilities, deliberately kept apart so no layer owns all of it:

| Responsibility | Example | Owner | Location |
|---|---|---|---|
| **The standard:** what quality must mean for everyone | Every data product declares freshness and completeness checks. PII columns have a masking rule. A product is certified only when its checks pass | Governance guild | `governance/standards/`, `governance/policies/` |
| **The mechanism:** the engine that runs checks and blocks a bad load | Test runner, quality gate, assertion results pushed to DataHub | Platform team | `platform/quality/` |
| **The specific rules:** the values for one dataset | `review_score` between 1 and 5, `order_id` not null, freshness ≤ 24 h | The domain | `domains/<domain>/contracts/` (ODCS) |

The platform's quality component is an **engine, not a policy**: it executes whatever rules a
contract declares and enforces the gate. The decision that certain checks are *mandatory*
lives in governance and is enforced in CI against every domain's contract (the rows above).
A domain is free to add stricter checks, never fewer.

### Where contracts live: two governed boundaries

A contract is an agreement at a **boundary where a producer hands data to a consumer**.
There are exactly two such boundaries: what a **source delivers** into a domain, and what a
domain **publishes** for others. The layers in between are internal and carry only
*engineering* quality checks, never a consumer-facing contract.

| Layer | Governed? | Contract | Quality | Consumed by |
|---|---|---|---|---|
| **Source**: a partner file in the landing zone, or a table of the shop's database (`shop_oltp`) | **Yes, governed boundary** | **ODCS** source contract, `domains/<domain>/contracts/sources/` (one folder whatever the transport, `sources.yml` says how each arrives) | Landing file: the gate checks it against its contract **before load**, or it is quarantined (proofs P2, P14). Database table: the loader governs and describes the raw table from its contract before the first row, and refuses a table whose columns no longer match | The owning domain, through its raw table |
| `<domain>_raw`, `<domain>_bronze/silver/gold` (internal medallion) | No contract of its own | none. A raw table carries its source contract's descriptions and tags | Engineering DQ, owned by the domain (dbt tests) | The owning domain only |
| **Output port**: a model tagged `output_port` in any layer | **Yes, governed boundary** | **ODCS** (table/view contract, carrying the `output_port` tag) | Contractual DQ declared in the contract, with results visible to consumers | Other domains, BI, AI (output ports only) |
| **Semantic output port** | **Yes, governed boundary** | **Apache Ossie** (semantic model) | Definitions resolve to governed concepts | BI and the AI agent (one metric definition for both) |

The semantic output port is the same boundary as the output port, published as a semantic model
instead of a table.

Every source has a contract, since 29 October 2026: three landing files (reviews, sellers,
geolocation) and six database tables (customers, orders, order_items, order_payments, products,
the category name translation). Before that only the landing files had one, and the database
tables' columns showed in the catalog with descriptions borrowed through lineage.

**Landing is not Raw.** Landing is the shared, governed file boundary where a partner's data
enters the platform. `<domain>_raw` is each domain's private copy after ingestion. Governing
every source with ODCS, files and database tables alike, is what makes consumption
*standardized* across domains.

ODCS therefore applies to **files, tables and views**, and Apache Ossie governs the **semantic
model**. Two kinds of quality run on top:

- **Engineering DQ:** internal, per-layer checks the domain owns (dbt tests), for its own
  raw/staging/models. Not visible to consumers.
- **Contractual DQ:** declared in the ODCS contract at a governed boundary, executed by the
  platform, and **visible to consumers** (landing gate results and output-port assertions).

And two kinds of ownership: **technical ownership** of every asset (who operates it) and
**governed product ownership** of a published output port (who is accountable for the promise
to consumers).

---

## 5. Warehouse layout and access

Each domain has its own medallion schemas in the `analytics` database and its own Postgres
role. **Output ports are not a separate schema.** They are models tagged `output_port`
(from their ODCS contract) that live in whichever layer produced them (ADR-0012).

| Schema | Contents | Who can read |
|---|---|---|
| `<domain>_raw` | Data as loaded from sources | Domain role only |
| `<domain>_bronze` | Standardized (typed) | Domain role only |
| `<domain>_silver` | Conformed (business rules, enrichment) | Domain role only |
| `<domain>_gold` | Business-ready (aggregates/marts) | Domain role only |
| *(any layer)* | A model tagged `output_port` is a **published port** | Domain role + declared consumer roles (per-model grant) |

An output port lives where it was produced (for example `sales_silver.orders` or `sales_gold.sales_performance`),
and consumers receive `USAGE` on that layer schema plus `SELECT` on the one port model. The
logical name (`sales.orders`) maps to the physical location through its data-product definition,
surfaced in the DataHub catalog (M4).

Cross-domain dbt references use [dbt-loom](https://github.com/nicholasyager/dbt-loom),
which lets one dbt Core project `ref()` another project's **public** output ports only.
