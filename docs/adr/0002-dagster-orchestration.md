# ADR-0002: Dagster for orchestration

**Status:** Superseded by [ADR-0005](0005-airflow-orchestration.md)

## Context
Data moves across three platforms (MinIO → Postgres `shop_oltp` → Postgres `analytics`)
and must run in order: land → quality gate → load → transform → test → publish metadata.
Failures must stop downstream steps, retry, and alert. Candidates: Apache Airflow, Dagster.

## Decision
Use **Dagster**.

## Rationale
- **Asset-based model:** Dagster schedules *datasets* (assets), not tasks, which maps
  directly to data products and to catalog lineage.
- **Data mesh fit:** one *code location* per domain. Each domain deploys its assets
  independently, the platform runs them all.
- **First-class dbt integration** (`dagster-dbt`): every dbt model becomes an asset.
- **Asset checks:** contract quality checks can block downstream assets, which is how
  the landing-zone quality gate is enforced.
- DataHub has a Dagster integration that emits run lineage.

## Consequences
- Airflow is more widespread in job descriptions, but the asset and quality-gate patterns
  used here transfer to Airflow 3 (assets) and OpenLineage.
- Dagster OSS has no built-in UI authentication, so production must put it behind SSO
  (see production-readiness.md).
