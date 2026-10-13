# Catalog capability — DataHub (M4)

DataHub is the platform's catalog: datasets, end-to-end + cross-domain lineage, domains, data
products, glossary, ownership, and quality/assertion results. It runs as the **`dgp-catalog`**
capability in the same Colima VM as the rest of the platform (ADR-0017), reachable at
http://localhost:9002 (dev login `datahub` / `datahub`).

## Prerequisites

```bash
uv tool install 'acryl-datahub[postgres,dbt]'   # the DataHub CLI + source plugins
```

## Runtime

| Command | What it does |
|---|---|
| `make catalog-up` | Start DataHub in the current Colima VM (quickstart). |
| `make catalog-down` | Stop DataHub (keeps its volumes). |
| `make catalog-ingest` | Regenerate dbt artifacts and push warehouse + dbt metadata into DataHub. |
| `make catalog-govern` | Push the mesh governance (domains, data products, ownership) from our definitions. |

> Quickstart is the *prove-it* deployment. It is replaced by a pinned `dgp-catalog` compose
> (images by digest, GMS remapped off `8080` so Airflow and DataHub coexist) — see the catalog ADR.

## Ingestion recipes (`recipes/`)

Ingestion runs on the host (where the dbt artifacts and the CLI live), reads the sources, and
pushes metadata to GMS. Secrets come from the environment (sourced from `.env`), never from the
recipe files.

- **`postgres_analytics.yml`** — the analytics warehouse: tables, columns, views across the
  per-domain `*_raw|_bronze|_silver|_gold` schemas.
- **`dbt.yml`** — one domain's dbt models, columns, tests and lineage. Run once per domain with
  `DBT_TARGET` pointing at that project's `target/`. `target_platform: postgres` links each dbt
  model to its warehouse dataset (sibling), so lineage joins across the two — including the
  dbt-loom cross-domain references (proof P7).

## Governance generator (`generate_governance.py`)

Technical metadata comes from the recipes above; the *governance* layer is generated from the
same source-of-truth definitions that drive grants and the data contracts — never hand-maintained
in the UI. `make catalog-govern` emits, idempotently:

- **Domains** — one per mesh domain; every warehouse + dbt dataset is filed under its domain.
- **Data products** — one per `domains/*/data_products/*.yaml`: display name, purpose, owner
  (corp group), and the output-port datasets (Postgres + dbt sibling) as assets, placed in the
  owning domain.
- **Classification glossary** — `PII` / `Internal` / `Public` terms, attached per column on each
  output port from the ODCS `classification` (e.g. `customer_pseudonym` → PII). Ties to ADR-0013.

dbt test results flow in as **assertions** on the datasets via the dbt recipe, so the contractual
DQ is visible in the catalog (the DataHub half of proof P2).
