# ADR-0012: Medallion layers, tag-based output ports, and a simulated marketplace

**Status:** Accepted
**Note:** the *simulated marketplace* (`make marketplace` → `docs/marketplace.md`) was retired at M4
and replaced by DataHub (see [ADR-0018](0018-catalog-deployment-and-catalog-as-code.md)). The
medallion / tag-based output-port decisions below still stand.

## Context
Domains need an internal engineering progression, and a clear, *deliberate* way to publish
data products. Two traps to avoid: equating "output port" with a layer ("Gold = published")
or with a dedicated schema (which re-couples publishing to a location). We also want to show
how a data product is *published for discovery*, like a Snowflake internal marketplace.

## Decision

### 1. Medallion internal layers
Each domain progresses `raw → bronze → silver → gold`, all **internal** (domain-role only,
engineering DQ):
- **raw**: as loaded (text). **bronze**: standardized (typed). **silver**: conformed
  (business rules, enrichment). **gold**: business-ready (aggregates/marts).

### 2. An output port is a **deliberately tagged model**, not a layer or a schema
A model is an output port **iff its ODCS contract carries the `output_port` tag**.
`datacontract dbt sync` propagates that tag onto the dbt model, so ODCS stays the single
source of truth. The tag is not hand-written in the dbt config. Consequences:
- **Gold ≠ Output Port.** Any layer's model can be a port (we publish `orders` from Silver and
  `sales_performance` from Gold), and an untagged Gold model is internal.
- Publishing is deliberate: it takes an output-port contract **and** a data product definition.
  The publishing guardrail (`check_output_ports.py`) enforces contract ⇔ model ⇔ definition.

### 3. Publish in place, with no dedicated output schema (Option B)
A port **lives in its medallion layer schema** (`sales_gold.sales_performance`,
`customer_silver.customers`). There is no `<domain>` output schema. Consumers are granted
`USAGE` on that layer schema + `SELECT` on the one port model (grants derived from the data
product definitions). The **marketplace listing** is the logical→physical abstraction
(`sales.sales_performance` → `sales_gold.sales_performance`).

### 4. Simulated internal marketplace
`make marketplace` generates `docs/marketplace.md` from the output-port contracts + data
product definitions, one *listing* per published port (purpose, owner, location, schema,
SLA, consumers). "Subscribing" is a consumer grant. This mirrors a Snowflake internal
marketplace. At M4 the same content is published to DataHub as the real catalog.

### 5. What stays dbt's, not governance's
`access: public` (dbt-loom cross-project visibility) and `materialized` (physical storage,
inherited from the layer folder: Silver → view, Gold → table) are dbt mechanics and remain
dbt config. Everything governance cares about (schema, classification, quality, SLA, and the
`output_port` tag) lives in ODCS.

## Consequences
- Publishing is a one-line, governed act (tag the contract) with no location coupling.
- Consumers read a port where it lives, and the marketplace + grants resolve logical → physical.
- Refactoring internal layers is free as long as the port's contract holds.
