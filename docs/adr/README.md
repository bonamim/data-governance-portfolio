# Architecture Decision Records

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-dbt-transformation.md) | dbt Core for transformation | Accepted |
| [0002](0002-dagster-orchestration.md) | Dagster for orchestration | Superseded by 0005 |
| [0003](0003-data-mesh-domains.md) | Organise the platform as a data mesh | Accepted |
| [0004](0004-colima-local-runtime.md) | Colima as the local container runtime | Accepted |
| [0005](0005-airflow-orchestration.md) | Apache Airflow 3 for orchestration | Accepted |
| [0006](0006-pipeline-state.md) | Pipeline state management | Accepted |
| [0007](0007-minio-image-source.md) | MinIO container image source | Accepted |
| [0008](0008-monorepo-module-boundaries.md) | Monorepo with enforced module boundaries | Accepted |
| [0009](0009-cloud-portability.md) | Cloud-portable by design | Accepted |
| [0010](0010-semantic-layer-ossie.md) | Apache Ossie as the semantic layer standard | Accepted |
| [0011](0011-landing-zone-slas-and-partitioning.md) | Landing-zone SLAs, partitioning, retention, shared-dataset ownership | Accepted |
| [0012](0012-medallion-and-output-ports.md) | Medallion layers, tag-based output ports, simulated marketplace | Accepted |
| [0013](0013-role-based-dynamic-masking.md) | Role-based dynamic data masking (ODCS-declared, per-platform) | Accepted |
| [0014](0014-flexible-write-audit-publish.md) | Flexible write-audit-publish (opt-in, severity-aware, materialization-aware) | Accepted (mechanism superseded by 0015) |
| [0015](0015-scaling-the-transform-layer.md) | Scaling the transform layer (Cosmos, per-model WAP, dbt state, microbatch backfill) | Proposed |
| [0016](0016-event-driven-ingestion.md) | Event-driven ingestion for partner-pushed files | Proposed |
| [0017](0017-local-runtime-topology.md) | Local runtime topology (1 VM = 1 environment, capability compose projects) | Accepted |
| [0018](0018-catalog-deployment-and-catalog-as-code.md) | Catalog deployment (pinned dgp-catalog compose) and catalog-as-code | Accepted |
| [0019](0019-breaking-change-management.md) | Breaking-change management (gate, dbt model versions, consumer approval) | Accepted |
| [0020](0020-governed-ai-mcp.md) | Governed AI through MCP (semantic computation + catalog discovery) | Accepted |
| [0021](0021-federated-domain-agents.md) | Federated domain agents + catalog-routed orchestrator (agentic mesh) | Accepted |
| [0022](0022-backups-and-restore-drill.md) | Backups and the automated restore drill (proof P9) | Accepted |
| [0023](0023-local-hardening-stand-ins.md) | Prove hardening locally where possible; specify the rest as desired controls | Accepted |
