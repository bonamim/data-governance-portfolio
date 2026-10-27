# ADR-0018: Catalog deployment and catalog-as-code

**Status:** Accepted
**Extends** [ADR-0017](0017-local-runtime-topology.md) (runtime topology), [ADR-0007](0007-minio-image-source.md)
(pin images by digest). **Supersedes** the simulated marketplace of [ADR-0012](0012-medallion-and-output-ports.md).

## Context
M4 introduces DataHub as the platform catalog. Three decisions needed settling: how it is deployed
locally, how its governance metadata is kept and versioned, and what happens to the simulated
marketplace (`docs/marketplace.md`) that stood in for a catalog until now.

## Decision

### 1. Deployment: a pinned `dgp-catalog` compose project
DataHub runs as the **`dgp-catalog`** compose project in the shared Colima VM (ADR-0017), vendored
from the DataHub quickstart at a fixed version with **all images pinned by digest** (ADR-0007).
**GMS is published on `8081`** so it does not collide with Airflow's apiserver on `8080`, so the two
run together. The quickstart CLI is a convenience for first bring-up only. The committed compose is
the source of truth. **Production deploys DataHub via its Helm chart on Kubernetes** with managed
Elasticsearch/Kafka and a secrets manager (docs/production-readiness.md), not this compose.

### 2. Catalog-as-code: git is the source of truth, DataHub is a read model
The catalog is a **projection of git**, not an authority:

- **Technical metadata** (datasets, columns, lineage, tests) comes from ingestion recipes over dbt
  artifacts + the warehouse (`make catalog-ingest`).
- **Governance** (domains, data products, ownership, the classification glossary) is **generated
  from the same definitions that drive grants and contracts**: `publish_governance.py` reading
  `domains/*/data_products/*.yaml` + the ODCS contracts (`make catalog-govern`).

So governed entities are **versioned as definitions in git and re-applied**, never hand-authored in
the UI as the system of record. The apply step runs on deploy (like a GitOps apply), and the catalog can
be rebuilt from git at any time. Changes are reviewed as PRs against the definitions, not as mutable
catalog state.

**Exception: human-curated metadata.** Freehand documentation, deprecation flags, and assertion
acknowledgements authored *in* DataHub exist only there. That makes DataHub's own store
(MySQL + OpenSearch) **backup-critical** for DR (docs/disaster-recovery.md). The generated layer is
reproducible, but the human layer is not.

### 2a. Lineage has one owner per hop (amended with the Cosmos transform DAGs)
- **Transformation lineage is dbt's** (`make catalog-ingest` over the dbt artifacts): raw → bronze →
  silver/gold, column level included.
- **Airflow adds only what dbt cannot see**: the ingestion tasks declare their inlets/outlets
  (source/landing → raw), and the DataHub plugin emits those.
- **The transform DAGs are not reported to DataHub** (`AIRFLOW__DATAHUB__DAG_FILTER_STR` denies
  `*_transform`). Cosmos attaches OpenLineage to every per-model task and the plugin reads it, which
  drew each dbt hop a second time through an Airflow node per model, and `enable_extractors` does not
  stop it on Airflow 3. Which DAG builds a dataset is a link on the dataset (`catalog-govern`), and
  transform runs are observed in Airflow.

### 3. Retire the simulated marketplace
`platform/marketplace/` and `docs/marketplace.md` were explicitly the stand-in "until DataHub at M4"
(ADR-0012). DataHub is now the real catalog and internal marketplace, so the generator, its `make`
target, and the CI drift-guard are removed. The data-product *definitions* remain the source of
truth, and DataHub renders them.

## Consequences
- The catalog is reproducible and coexists with Airflow, and the deployment is committed and pinned.
- Governance is diff-reviewable and re-runnable. Drift between git and the catalog is closed by
  re-running the apply, not by editing the UI.
- DR must cover DataHub's store for human-curated metadata, but the generated layer needs no backup.
- One less generator/CI gate to maintain, and the marketplace view lives in DataHub.
