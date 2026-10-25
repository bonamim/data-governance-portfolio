# platform/core/orchestration (`dgp-platform-orchestration`)

Orchestration capability. Provides Airflow 3 **DAG factories** so each domain gets one DAG
bundle without writing orchestration code (ADR-0005, ADR-0008). Airflow itself is provided by
the runtime image, not by this package, so the local dev venv stays light.

## One DAG bundle per domain

`platform/core/orchestration/docker-compose.airflow.yml` registers one `LocalDagBundle` per domain,
each pointing at that domain's `pipelines/` folder. A domain's DAG file is a single call:

```python
# domains/sales/pipelines/sales_ingestion.py
from dgp_platform_orchestration.ingestion_dag import build_ingestion_dag

dag = build_ingestion_dag("sales")
```

The factory reads the domain's `sources.yml` and builds the right task graph:

| Domain shape | Scheduled on | Tasks generated |
|---|---|---|
| OLTP sources only (e.g. customer) | `shop-oltp` | `load_raw` |
| Landing sources only (e.g. customer_experience) | `landing/<domain>` (arrivals) | `contract_gate → load_raw`, `contract_gate → publish_quality` |
| Both (e.g. marketplace) | `shop-oltp` or `landing/<domain>` | `contract_gate → load_raw`, `contract_gate → publish_quality` |

`landing/<domain>` is fired by an event trigger on the arrival queue (ADR-0016): the object
store notifies it on every upload into the domain's inbox prefixes, and one burst is one run.
The PoC source simulators (`seed`, partner `deliver`) live in the platform DAG
`platform_source_simulator`, not in the domain DAGs; `platform_landing_sla` checks delivery
deadlines every 15 minutes and alerts the owning domain.

`contract_gate` checks every inbox file against its contract and files it to `accepted/` or
`quarantine/`; it loads nothing. `load_raw` loads only accepted files no load has picked up yet, so
it can be retried without re-checking (ADR-0025).

## Transform DAGs (ADR-0015)

`domains/<d>/pipelines/<d>_transform.py` is one call to `build_transform_dag(domain)`. Cosmos
renders the dbt project from its manifest (`make dbt-manifests`, run by `make airflow-up`):

| Task group | What runs |
|---|---|
| `models` | one `run` + `test` group per model, except opted-in ports and their descendants |
| `wap_<model>` | per opted-in port: `plan → stage → write → audit_view → audit → publish`, `quarantine` on failure |
| `downstream` | models that read an opted-in port, after its publish |
| `grant` | masking views + consumer grants (always) |
| `save_state` | manifest → `platform-state`; emits `published/<domain>` only if everything published |

Backfill = trigger the DAG with params `event_time_start` / `event_time_end` (or `full_refresh`).
The steps are the same ones `make publish` runs (`dgp_platform_orchestration.wap`).

## Assets and triggering

- `platform_acquire_olist` (platform bundle) downloads the Olist dataset and publishes the
  `olist-cache` asset.
- Each domain DAG is `schedule=[olist-cache]`, so it runs when the producer updates — the
  asset-driven cross-domain pattern behind proof P4 — and publishes its own `raw/<domain>`
  asset for later transform DAGs.

## Why a separate capability package

Keeping orchestration out of `platform/core/ingestion` means the orchestrator is swappable: all
governance logic stays in orchestrator-agnostic platform code, and only this package and the
DAG files change if Airflow is ever replaced (ADR-0005). The boundary check (ADR-0008) allows
`domains → platform` imports but forbids the reverse.
