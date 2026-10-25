# Semantic layer — `platform/context/semantics`

The platform capability that turns the domains' Apache Ossie semantic models
(`domains/<d>/semantics/*.semantic_model.yaml`, ADR-0010) into what each consumer computes — so a
metric has **one governed definition**. Today the consumer is Superset; the M7 AI agent reads the
same specs.

The design is **define-once, project-into-each-tool** (not a runtime semantic service like Cube):
Ossie is the source of truth, and `publish_superset.py` projects it into Superset's native dataset
+ metrics. The drift check keeps that projection honest.

`ossie.py` is the **shared reader** — the one place a semantic model becomes SQL. Both consumers go
through it: this Superset adapter and the M7 governed AI (`ai/semantic_mcp`, ADR-0020). Because there
is no second parser, "one governed definition per metric" holds in code, and proof P10
(`make prove-p10`) shows BI and AI return the same number.

## Fields: contract columns or derived from them

A field (dimension) is either a column of the port's ODCS contract, named as that column, or a
**derived** field with its own name whose expression references only contract columns as
`<dataset>.<column>`, e.g. `order_purchase_month` =
`DATE_TRUNC('month', orders.order_purchase_timestamp)` in `domains/sales/semantics/orders`.
`check-semantics` enforces this, and `check-ai-agents` blocks a derived field (or metric) that
references a PII column, so a new grain never needs a physical column and never wraps PII. In
Superset, group such a dataset by the source column with Superset's own time grain; the AI path
compiles the derived expression.

## Commands

| Command | What it does |
|---|---|
| `make bi-publish` | Publish each Ossie model to Superset — the warehouse connection, the dataset, and its metrics, generated from the spec (not hand-typed). Idempotent. |
| `make bi-check-drift` | **Fail if Superset's metrics no longer match the Ossie models** (e.g. edited in the UI). This is what makes "one definition" binding rather than a one-time copy. |
| `make ossie-build` | Build the Ossie bundle in `build/ossie/`: the ontology (each value type with the description its domain keeps in `domains/<d>/contracts/definitions/`), the compiled semantic models and the generated mappings — every document checked with Ossie's own validator (`vendor/ossie/`, ADR-0027). |

## Switching the warehouse (e.g. to Snowflake with views)

The semantic layer is **engine-agnostic by design** — the metric expressions are **ANSI SQL** and the
adapter never hardcodes a warehouse. Moving to Snowflake, with Snowflake views as the output ports,
changes only the edges:

1. **Ossie models** — unchanged metric logic (`SUM(...)`, `AVG(...)` are ANSI). Point each dataset's
   `source` at the Snowflake `<database>.<schema>.<view>`. Ossie's per-dialect `expression` list
   (`dialect: ANSI_SQL` / `SNOWFLAKE_SQL`) is there precisely to carry any engine-specific metric.
2. **Superset driver** — add `snowflake-sqlalchemy` to `reporting/Dockerfile` (exactly as it adds
   `psycopg2-binary` for Postgres).
3. **Connection** — set `SUPERSET_WAREHOUSE_URI` to
   `snowflake://user:pw@account/db/schema?warehouse=...&role=...`. The adapter and the generated
   metrics are unchanged.
4. **Lineage** — set `WAREHOUSE_PLATFORM=snowflake` so `make catalog-govern` links the Superset
   dataset to the Snowflake table in DataHub (instead of Postgres).

**One real gotcha — identifier casing.** Snowflake folds unquoted identifiers to UPPERCASE. Create
the views with lowercase (quoted) names, or set the Ossie `source`/columns to match how the objects
are actually stored, so the dataset + metric columns resolve.

Nothing in the adapter, the Ossie models, or the drift check is Postgres-specific — only the
`reporting/Dockerfile` driver, `SUPERSET_WAREHOUSE_URI`, and `WAREHOUSE_PLATFORM` change. That is the
cloud-portability thesis (ADR-0009) applied to the semantic + BI layer.
