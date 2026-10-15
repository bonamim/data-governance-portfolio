# Standard: what makes a valid data product

Portable governance standard (the **WHAT**, above the governance boundary). It defines the
required shape of a published data product and the heuristics it must satisfy, based on the
[data-mesh-architecture heuristics](https://www.datamesh-architecture.com/data-product-heuristics).
The platform enforces the machine-checkable parts in CI; the rest are review guidance for the
publishing guardrails.

## 1. Every data product declares a definition

`domains/<domain>/data_products/<name>.yaml`:

```yaml
name: sales_performance
type: aggregate            # source-aligned | aggregate | consumer-aligned
purpose: "Report order and revenue performance by customer location and seller."  # verb + object, one sentence
owner:
  team: sales
  accountable: "<person or role>"
consumers:                 # at least one real consumer with a use case
  - team: marketing
    use_case: "Target campaigns by region performance."
output_ports:              # one authoritative definition per concept
  - kind: table            # table | view | semantic
    contract: domains/sales/contracts/sales_performance.odcs.yaml
inputs:                    # the data products this one consumes (never raw/staging)
  - customer.customers
  - sales.orders
publish:                   # optional (ADR-0014). Default: publish in place.
  audit: required          # required = write-audit-publish (critical products only)
```

## 2. Required (CI-checkable)

| Rule | Check |
|---|---|
| `purpose` present, one sentence, starts with a verb | `governance/policies/check_data_products.py` (M3) |
| `type` is one of the three | same |
| `owner.team` present and is a known domain | same |
| at least one `consumer` with a `use_case` | same ("without a use case there is no data product") |
| every `output_port` points at a valid ODCS contract | same + `datacontract lint` |
| `inputs` reference data products (output ports), never raw/staging | same |
| aggregate products declare **≥ 3** consuming teams | same |
| **output-port completeness** — the output-port contract's table has a `description`, and every column has a `description` and a `classification` | `governance/policies/check_output_ports.py` (proof P12) |
| every Ossie semantic dataset, field and metric has a `description` (it is what the AI agent reads) | `governance/policies/check_semantics.py` (proof P12) |

An output port is an interface consumers read **without asking the owner** — people in the
catalog, Superset, and AI agents through the semantic MCP. A column with no description is a
promise nobody made: a person can ask, an agent guesses from the name and answers confidently.
So completeness blocks publishing; it is not a warning. (`datacontract lint` does not cover it:
ODCS makes `description` optional.)

## 3. Heuristics (review guidance in the publishing guardrails)

- **Consistent quality across output ports** — a table, view and semantic port of the same
  product share one meaning and one set of quality rules.
- **Low integration burden** — a consumer should not have to stitch several products together
  to answer a simple question; if they do, the boundary is wrong.
- **Bounded scope / smallest useful unit** — decompose by domain and process, not by source
  system or artifact type.
- **Source-aligned:** semantic coherence (one meaning per concept), single business domain,
  contained source blast radius.
- **Aggregate:** general-purpose content only, dedicated ownership, never a mini-warehouse.
- **Consumer-aligned:** one decision context, boundaries following process lines, a named
  business team, only what that team needs.

## 4. Ownership vs. consumption (ties to ADR-0011)

One accountable owner per product; many consumers. A shared source dataset is **source-aligned
and singly owned** (e.g. `reference.geolocation`), not an aggregate — an aggregate only exists
when 3+ teams need the same derived facts and someone owns that derivation.
