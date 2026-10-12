# Production Readiness

The local Docker setup is optimised for a laptop: default ports, local volumes, plain HTTP.
**None of these defaults are safe for production.** This guide lists, component by
component, what must change before the platform holds real data.

Status column: ⬜ not implemented yet · ✅ implemented and documented in the repo.
Each item is completed in the milestone where the component is introduced.

---

## 1. Cross-cutting requirements

| # | Requirement | How | Status |
|---|---|---|---|
| X1 | **No secret in git** | `.env` is gitignored; production reads secrets from a secrets manager (HashiCorp Vault, AWS Secrets Manager, Azure Key Vault or GCP Secret Manager) | ⬜ |
| X2 | **No default credential** | Every default admin user/password is replaced at deploy time; startup fails if a known default is detected | ⬜ |
| X3 | **TLS everywhere** | TLS on every endpoint, terminated at a reverse proxy (Traefik/NGINX) for UIs; native TLS for Postgres and MinIO | ⬜ |
| X4 | **Single sign-on** | DataHub, Superset and Airflow behind OIDC (Keycloak, Okta, Entra ID, Google) | ⬜ |
| X5 | **Least privilege** | One service account per component and per domain; no component uses a superuser | ⬜ |
| X6 | **Network isolation** | Only the reverse proxy is exposed; databases, MinIO API, Kafka and Elasticsearch are on an internal network | ⬜ |
| X7 | **Encryption at rest** | Encrypted disks/volumes; MinIO server-side encryption | ⬜ |
| X8 | **Pinned versions** | Every image pinned to a version (never `latest`); Python dependencies locked with `uv.lock` | ⬜ |
| X9 | **Audit logging** | Access logs from Postgres, MinIO, DataHub and Superset shipped to central logging | ⬜ |
| X10 | **Monitoring & alerting** | Health checks on every service; alerts on failed runs, failed quality checks, SLA breaches, low disk, failed backups | ⬜ |
| X11 | **Backups & tested restores** | See [Disaster recovery](disaster-recovery.md) | ⬜ |

---

## 2. Component configuration

### MinIO — landing zone

| Setting | Local | Production |
|---|---|---|
| Root credentials | From `.env` | From secrets manager; root user never used by applications |
| Application access | — | One MinIO user per writer/reader with a bucket-scoped policy (e.g. `landing-writer`, `landing-reader`) |
| Public access | None | None — anonymous bucket policies forbidden |
| Versioning | On | On for every bucket (recovers overwritten or deleted files) |
| Object lock | Off | On (governance mode) for the landing bucket — raw files are immutable evidence |
| Encryption | Off | SSE with an external KMS (MinIO KES) |
| Replication | Off | Site or bucket replication to a second location |
| Lifecycle | None | Expire noncurrent versions after the retention period |
| TLS | Off | On |

### PostgreSQL — `shop_oltp` and `analytics`

| Setting | Local | Production |
|---|---|---|
| Authentication | `scram-sha-256` | `scram-sha-256`; `pg_hba.conf` restricted to known subnets; `hostssl` only |
| TLS | Off | `ssl = on`, certificates from your CA |
| Roles | Per domain, created by init script | Same, plus: no application uses `postgres`; `CONNECT` revoked from `PUBLIC` |
| Domain isolation | Per-domain schemas and grants | Same, generated from data product definitions (see [data-mesh.md](data-mesh.md)) |
| PII | Classified in contracts | Classified + column masking views or `pgcrypto` for PII; consumers see masked columns unless granted |
| Backups | Optional | pgBackRest: full + incremental backups and continuous WAL archiving (see DR plan) |
| High availability | Single node | Primary + streaming replica (Patroni) or a managed service (RDS, Cloud SQL, Azure Database) |
| Logging | Default | `log_connections`, `log_disconnections`, `pgaudit` for DDL and role changes |

### Apache Airflow 3 — orchestration

| Setting | Local | Production |
|---|---|---|
| Metadata database | Postgres | Dedicated Postgres, backed up — holds run state and data intervals ([ADR-0006](adr/0006-pipeline-state.md)) |
| Auth manager | Simple auth manager (development only) | FAB auth manager with OIDC/OAuth SSO, roles per domain team |
| `fernet_key` | From `.env` | From secrets manager — encrypts connection passwords in the metadata DB; rotated with `airflow rotate-fernet-key` |
| API / JWT secrets | From `.env` | Long random values from secrets manager, identical across all API server instances |
| Connections & variables | Environment variables | Secrets backend (`[secrets] backend` → Vault, AWS Secrets Manager, GCP, Azure) — never stored in the metadata DB or in DAG code |
| Executor | LocalExecutor | KubernetesExecutor or CeleryExecutor |
| Domain isolation | One DAG bundle per domain; domain tasks in their own image | Same, with per-domain images and Kubernetes namespaces/service accounts |
| Components | Single replica each | API server and scheduler with ≥2 replicas; separate DAG processor and triggerer |
| Retries & alerts | Basic | Retries per task; failure and SLA-miss callbacks posting to the on-call channel |

### DataHub — catalog

| Setting | Local | Production |
|---|---|---|
| Default `datahub` user | Present | Removed or password changed; SSO enabled |
| Metadata service auth | Off | `METADATA_SERVICE_AUTH_ENABLED=true`; ingestion uses personal access tokens scoped to a service account |
| SSO | Off | OIDC (`AUTH_OIDC_ENABLED=true` and related settings) |
| Authorization | Default | Policies per domain: domain owners edit their own assets only |
| Storage | Quickstart containers | Managed or HA MySQL/Postgres, Elasticsearch/OpenSearch and Kafka |
| Metadata as code | Recipes in repo | Same — domains, glossary and data products re-applied from git on deploy |

### Apache Superset — reporting

| Setting | Local | Production |
|---|---|---|
| `SECRET_KEY` | From `.env` | Long random value from secrets manager; Superset refuses to start with the default |
| Admin user | Created at init | Replaced by SSO; no shared admin account |
| Database connection | Read-only role | Read-only role per consumer, `SELECT` on data product schemas only |
| Row-level security | Off | On where needed (e.g. sellers see their own data) |
| Dashboards | Exported as code | Same — dashboards versioned in `reporting/` and deployed from git |
| Metadata database | Postgres | Dedicated Postgres, backed up |

### CI/CD — GitHub Actions

| Setting | Production |
|---|---|
| Branch protection | `main` protected: PR + review + passing contract checks required |
| CODEOWNERS | Each `domains/<domain>/` owned by its team; `governance/` owned by the governance guild |
| Secrets | GitHub environments with required reviewers; OIDC federation to cloud instead of long-lived keys |
| Action pinning | Third-party actions pinned to a commit SHA |
| Dependency scanning | Dependabot (or Renovate) for Python, Docker images and actions |

### AI agent

| Setting | Production |
|---|---|
| Data access | Through DataHub MCP and certified data products only — no direct access to raw schemas |
| Credentials | Dedicated read-only DataHub token and Postgres role |
| PII | PII-classified columns are excluded from the agent's role |
| Logging | Every question, tool call and generated query logged for audit |

---

## 3. Go-live checklist

Before promoting to production, every row in section 1 and every "Production" column above
must be met, and the latest **restore drill** must have passed (see the DR plan).
