# ADR-0003: Organise the platform as a data mesh

**Status:** Accepted

## Context
Centralised data teams become bottlenecks, and ownership of data quality is unclear.
The project should show how governance works when ownership is distributed.

## Decision
Split the platform into **domains** (Sales, Customer, Marketplace, Customer Experience),
a **self-serve platform** (`platform/`) and **federated governance** (`governance/`).
Domains share data only through contract-backed **data products** (output ports).

## Consequences
- Each domain owns its contracts, dbt project and data products (enforced by CODEOWNERS).
- Global rules are enforced in CI, not by manual review.
- More moving parts than a single dbt project — acceptable, as this is the point of the
  demonstration. See [data-mesh.md](../data-mesh.md).
