# platform/orchestration (`dgp-platform-orchestration`)

Orchestration capability. Provides Airflow 3 **DAG factories** so each domain gets one DAG
bundle without writing orchestration code (ADR-0005, ADR-0008). Airflow itself is provided by
the runtime image, not by this package, so the local dev venv stays light.

## One DAG bundle per domain

`platform/orchestration/docker-compose.airflow.yml` registers one `LocalDagBundle` per domain,
each pointing at that domain's `pipelines/` folder. A domain's DAG file is a single call:

```python
# domains/sales/pipelines/sales_ingestion.py
from dgp_platform_orchestration.ingestion_dag import build_ingestion_dag

dag = build_ingestion_dag("sales")
```

The factory reads the domain's `sources.yml` and builds the right task graph:

| Domain shape | Tasks generated |
|---|---|
| OLTP sources only (e.g. customer) | `seed → load_raw` |
| Landing sources only (e.g. customer_experience) | `land → contract_gate → load_raw`, `contract_gate → publish_quality` |
| Both (e.g. marketplace) | `seed → load_raw`, `land → contract_gate → load_raw`, `contract_gate → publish_quality` |

`contract_gate` checks every inbox file against its contract and files it to `accepted/` or
`quarantine/`; it loads nothing. `load_raw` loads only accepted files no load has picked up yet, so
it can be retried without re-checking (ADR-0025).

## Assets and triggering

- `platform_acquire_olist` (platform bundle) downloads the Olist dataset and publishes the
  `olist-cache` asset.
- Each domain DAG is `schedule=[olist-cache]`, so it runs when the producer updates — the
  asset-driven cross-domain pattern behind proof P4 — and publishes its own `raw/<domain>`
  asset for later transform DAGs.

## Why a separate capability package

Keeping orchestration out of `platform/ingestion` means the orchestrator is swappable: all
governance logic stays in orchestrator-agnostic platform code, and only this package and the
DAG files change if Airflow is ever replaced (ADR-0005). The boundary check (ADR-0008) allows
`domains → platform` imports but forbids the reverse.
