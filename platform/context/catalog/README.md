# Catalog capability — DataHub (M4)

DataHub is the platform's catalog: datasets, end-to-end and cross-domain lineage, domains, data
products, the business glossary, ownership, and quality results. It runs as the **`dgp-catalog`**
compose project, pinned by digest, in the same Colima VM as the rest of the platform
(ADR-0017, ADR-0018). The UI is at http://localhost:9002 (local login `datahub` / `datahub`); GMS,
the metadata service, is on port 8081 so it doesn't collide with Airflow's 8080.

```
platform/context/catalog/
  docker-compose.datahub.yml        the pinned DataHub deployment
  recipes/                          DataHub's own ingestion: technical metadata
  src/dgp_platform_catalog/
    publish_governance.py           the governance layer, from the definitions in git
    publish_landing_quality.py      the landing datasets and every contract-gate result
    gms.py                          a small read client for GMS (search, aspects, GraphQL)
  tests/
```

## Prerequisites

```bash
uv tool install 'acryl-datahub[postgres,dbt]'   # the DataHub CLI and source plugins, for the recipes
```

The publishers need the DataHub SDK too; `uv run --group catalog` provides it (the `publish` extra
of this package), and the Make targets use it.

## Commands

| Command | What it does |
|---|---|
| `make catalog-up` | Start DataHub. |
| `make catalog-down` | Stop DataHub (its volumes are kept). |
| `make catalog-ingest` | Regenerate the dbt artifacts, then run the recipes: warehouse, OLTP and dbt metadata. |
| `make catalog-ingest-bi` | Run the Superset recipe: BI datasets and charts. |
| `make catalog-govern` | Publish the governance layer from the definitions (below). |
| `make catalog-quality` | Publish the landing datasets and the contract gate's results (below). |

## Technical metadata: the recipes (`recipes/`)

Ingestion runs on the host, where the dbt artifacts and the CLI are. It reads the sources and pushes
metadata to GMS. Secrets come from the environment (sourced from `.env`), never from a recipe.

- **`postgres_analytics.yml`** — the warehouse: tables, columns and views across every domain's
  `*_raw`, `*_bronze`, `*_silver` and `*_gold` schemas.
- **`postgres_oltp.yml`** — the operational source, `shop_oltp`, where lineage starts for the
  tables loaded from it.
- **`dbt.yml`** — one domain's dbt models, columns, tests and lineage. It runs once per domain, with
  `DBT_TARGET` pointing at that project's `target/`. `target_platform: postgres` links each dbt
  model to its warehouse dataset, so lineage joins across the two, including dbt-loom's
  cross-domain references (proof P7).
- **`superset.yml`** — the BI datasets and charts, so lineage reaches the chart
  (`make catalog-ingest-bi`, once Superset is up and published).

No recipe profiles with sample values: the catalog never holds raw column values (proof P21 checks
this).

## The governance layer (`publish_governance`)

The recipes bring in technical metadata. The *governance* on top of it is generated from the same
definitions that drive the grants and the contracts, and is never maintained by hand in the UI.
`make catalog-govern` publishes, idempotently:

- **Domains** — one per mesh domain; every warehouse and dbt dataset is filed under its domain.
- **Data products** — one per `domains/*/data_products/*.yaml`: display name, purpose, owner, and
  the output-port datasets (the Postgres table and its dbt sibling) as its assets.
- **Meaning** (ADR-0027) — the business glossary (entity types, and the value types the ontology
  gives a business definition, grouped by the domain responsible for each), and the data
  dictionary: on every output-port column, the data element it holds, its business term and its
  governed tags (sensitivity, privacy category).
- **Deprecation** — a port version being retired carries its sunset date and its successor
  (ADR-0019).
- **Where each dataset is built** — a link from every warehouse dataset to the Airflow DAG that
  builds it, kept as node metadata rather than a lineage edge (ADR-0018).
- **BI lineage** — each Superset dataset is linked to the output port it reads.

With a warehouse that has native tags (Snowflake, Databricks, BigQuery), the governed tags are set
by the dbt build and ingested from the warehouse instead (`WAREHOUSE_PLATFORM`).

## Landing quality (`publish_landing_quality`)

For every landing dataset in a `sources.yml`, this publishes the S3 dataset with its contract's
schema and operational rules, its lineage into `<domain>_raw`, one assertion per rule the gate
runs, one result per delivered file and rule (from the ingestion ledger), and one arrival
operation per file, timed when it landed, so freshness is measured from arrival (ADR-0016,
ADR-0024). S3 datasets that are no longer landing datasets are soft-deleted, after each URN is
checked to be an S3 dataset, since GMS may ignore a search's platform filter.

It runs after every contract gate, as each ingestion DAG's `publish_quality` task (`--domain`), and
in full with `make catalog-quality`.
