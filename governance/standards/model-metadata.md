# Standard: dbt model metadata

How model metadata is organised so that **generated** (output-port) and
**hand-authored** (internal) content never collide, and the warehouse stays
documented as it grows. A convention, not a CI gate: teams own their internal
models (ADR-0012).

## Two files per layer, two owners

Each medallion layer folder (`bronze/`, `silver/`, `gold/`) keeps metadata in two
separate files:

| File | Owner | Contents | Edited by |
|---|---|---|---|
| `_ports.yml` | the platform (generated) | output-port models: schema, classification-driven tests, the `output_port` tag, all from the ODCS contract | **never by hand**: `make dbt-ports` regenerates it |
| `_models.yml` | the domain (hand) | internal (non-port) models: a description and key tests | the domain team, directly |

They describe disjoint sets of models, so neither tool nor human steps on the other.

## Minimum metadata for an internal model

When a domain adds an internal model, it should add to the layer's `_models.yml`:

- a one-line **`description`** (so the model is not blank in the catalog / DataHub at M4)
- **`not_null`** (and `unique` for a primary key) on the key columns.

That is the whole obligation for internal models. They carry no consumer promise,
so no ODCS contract, no classification, no SLA.

## Promoting an internal model to an output port

Publishing is one deliberate step: write an **ODCS output-port contract** for the
model (with `tags: [output_port]`) and a **data product definition**. `make
dbt-ports` then seeds and fills the model's entry in `_ports.yml`, and the
publishing guardrail (`check_output_ports.py`) starts enforcing it.

Unlike an internal model, a port's metadata **is** a CI gate: the contract must describe the
table (`schema[].description`, synced to the dbt model description) and every column (a
`description` and a `classification`). See *output-port completeness* in
[data-product.md](data-product.md). The model keeps
living in whatever layer it already was: Bronze, Silver or Gold.
