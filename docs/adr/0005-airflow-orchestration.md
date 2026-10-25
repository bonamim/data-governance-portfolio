# ADR-0005: Apache Airflow 3 for orchestration

**Status:** Accepted — supersedes [ADR-0002](0002-dagster-orchestration.md)

## Context
ADR-0002 chose Dagster for its asset model, per-domain code locations and blocking asset
checks. In July 2026 Prefect announced the acquisition of Dagster Labs. Prefect committed to
keep Dagster's name, open-source license and roadmap, but Dagster is now owned by the vendor
of a competing product. This project's goal is a platform that can be promoted to
production safely, so long-term independence of core components is a requirement.

## Options compared

| Criterion | Dagster | Airflow 3 |
|---|---|---|
| Per-domain deployable unit | Code locations (native) | DAG bundles + containerised tasks |
| Blocking quality checks | Asset checks (native) | Quality task fails → downstream skipped, asset not emitted |
| Data-product (asset) model | Core concept | Assets since 3.0; task-centric core |
| Cross-domain triggers | Declarative automation | Asset-triggered DAGs, asset watchers |
| DataHub lineage | Integration available | Native OpenLineage + mature DataHub plugin |
| Governance of the project | Vendor-owned (Prefect) | Apache Software Foundation |
| Managed production options | Dagster+ | MWAA, Cloud Composer, Astronomer, Helm chart |
| Market adoption | Growing | Industry standard |

## Decision
Use **Apache Airflow 3**. Keep all governance logic (contract validation, quality gate,
lineage emission, publishing) in `platform/` as orchestrator-agnostic Python/CLI, so the
orchestrator only sequences work and stops on failure.

## Closing Dagster's advantages
Each gap is closed by design and **proven by a reproducible scenario** in
[docs/proofs.md](../proofs.md):

| Dagster advantage | Airflow 3 design | Proof |
|---|---|---|
| Code locations | One DAG bundle per domain; domain tasks run in their own container image | P1 |
| Blocking asset checks | Quality-gate task before every load and publish; failure quarantines input and emits no asset event | P2, P3 |
| Asset-centric view | Results published to DataHub as assertions on each dataset | P2, P7 |
| Declarative cross-domain automation | Consumer DAGs scheduled on producer data-product assets | P4 |

## Consequences
- More platform code than Dagster would need — acceptable, and orchestrator-portable.
- Switching orchestrator later only touches `platform/core/orchestration/` and the DAG files.
