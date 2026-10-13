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
