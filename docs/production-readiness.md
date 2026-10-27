# Production Readiness

The local Docker setup is optimised for a laptop: default ports, local volumes, plain HTTP.
**None of these defaults are safe for production.** This guide lists, component by
component, what must change before the platform holds real data.

Status column: ✅ implemented and enforced in the repo · 📄 production-only control, with the exact
configuration in [§4](#4-reference-production-configuration) (local uses a stand-in) · ⬜ not done yet.
Each item is completed in the milestone where the component is introduced. M8 closed the
cross-cutting rows.

---

## 1. Cross-cutting requirements

| # | Requirement | How | Status |
|---|---|---|---|
| X1 | **No secret in git** | `.env` is gitignored, and `make preflight` flags secrets hard-coded as literals in compose files. Secrets manager: §5 | ✅ git · 📄 manager |
| X2 | **No default credential** | `platform/core/infra/preflight.py`: unset, `.env.example`, well-known (`admin`, `datahub`, …), `local_`/`dev_only` placeholders, < 16 chars, default admin user names, compose literals. Warns on `make up`. **`DGP_ENV=prod` makes it fail**, and the deploy pipeline runs it before rollout. Unit-tested, P13 | ✅ |
| X3 | **TLS everywhere** | **Postgres: TLS-only.** Every instance (oltp, analytics, Airflow metadata, drill sandbox) runs `ssl=on`, TLS ≥ 1.2, `hostssl` + scram for network clients and **plain TCP rejected** (`platform/core/infra/postgres/pg_hba.conf`). Host tools, Airflow and pgAdmin connect `verify-full` against a local CA (`make certs`). P13. UIs, MinIO and remote MCP behind a TLS reverse proxy: §5 | ✅ Postgres · 📄 proxy |
| X4 | **Single sign-on** | DataHub, Superset, Airflow and remote MCP behind OIDC: §5 | 📄 |
| X5 | **Least privilege** | No automation uses a superuser. Data plane: a role per domain, `platform_loader`, `oltp_reader`/`oltp_loader`, read-only `analytics_reader`, `compliance` unmask role, **one `agent_<domain>` role per AI agent** (granted only its domain's ports). Admin plane: **`access_admin`** applies masking + grants, **`catalog_reader`** (`pg_read_all_data`) feeds DataHub. `CONNECT` revoked from `PUBLIC`. P8, P11, P13. Operators keep socket-only break-glass access inside the container | ✅ |
| X6 | **Network isolation** | Only the reverse proxy is exposed, with data stores on an internal network: §5. Local: single-user laptop (Colima forwards ports to the host loopback), and the drill sandbox binds `127.0.0.1` | 📄 |
| X7 | **Encryption at rest** | **MinIO SSE-S3 on every bucket** (built-in KMS, `MINIO_KMS_SECRET_KEY`), existing objects migrated (`make encrypt-at-rest`), backups encrypted with their bucket. P13. External KMS (KES), encrypted volumes, pgBackRest cipher: §5 | ✅ objects · 📄 KMS/disks |
| X8 | **Pinned versions** | Every compose image and Dockerfile base pinned by `@sha256` digest and **every GitHub Action pinned to a commit SHA**, enforced in CI (`check-pinned-images`). Python locked with `uv.lock` | ✅ |
| X9 | **Audit logging** | **Postgres** `log_connections`, `log_disconnections`, `log_statement=ddl` on every instance, plus the AI audit log `{agent, tool, allowed/refused, sql}` (ADR-0021). P13. `pgaudit`, MinIO audit webhook and shipping to a central store: §5 | ✅ local · 📄 shipping |
| X10 | **Monitoring & alerting** | Health checks on every compose service. **`make dr-status`** fails when the latest backup is stale (> 26 h) or no drill passed in 7 days, which are the DR alert conditions. Metrics, alert routing, paging: §5 | ✅ DR checks · 📄 alerting |
| X11 | **Backups & tested restores** | `make backup` + automated restore drill, **proof P9**: physical base backup + **continuous WAL archiving** (`archive_timeout=60s`) with **point-in-time recovery** proven in the drill (RPO measured), plus dumps of every metadata store (ADR-0022, [DR plan](disaster-recovery.md)) | ✅ |

---

## 2. Component configuration

### MinIO: landing zone

| Setting | Local | Production |
|---|---|---|
| Root credentials | From `.env` | From secrets manager. The root user is never used by applications |
| Application access | none | One MinIO user per writer/reader with a bucket-scoped policy (e.g. `landing-writer`, `landing-reader`) |
| Public access | None | None. Anonymous bucket policies are forbidden |
| Versioning | On | On for every bucket (recovers overwritten or deleted files) |
| Object lock | On for `landing-archive`, per accepted object, GOVERNANCE (ADR-0024) | Same, COMPLIANCE mode, so accepted files are immutable evidence |
| Encryption | SSE-S3 on every bucket (built-in KMS, static key) | SSE with an external KMS (MinIO KES) |
| Replication | Off | Site or bucket replication to a second location |
| Lifecycle | None | Expire noncurrent versions after the retention period |
| TLS | Off | On |

### PostgreSQL: `shop_oltp` and `analytics`

| Setting | Local | Production |
|---|---|---|
| Authentication | `scram-sha-256` | `scram-sha-256`, `pg_hba.conf` restricted to known subnets, `hostssl` only |
| TLS | `ssl = on`, TLS ≥ 1.2, `hostssl` only (plain TCP rejected), local CA, clients `verify-full` | Same, certificates from your CA / cert-manager |
| Roles | Per domain, `analytics_reader`, `agent_<domain>`, `compliance`, `access_admin` (admin plane), `catalog_reader`. No automation uses `postgres`, and `CONNECT` is revoked from `PUBLIC` | Same, and `compliance` becomes a `NOLOGIN` group granted to named users |
| Domain isolation | Per-domain schemas and grants | Same, generated from data product definitions (see [data-mesh.md](data-mesh.md)) |
| PII | Classified + **role-based masking** enforced (ADR-0013): consumers read masking views (`<domain>_masked`), raw/staging is denied, and `compliance` unmasks. Proof P8 | Same policy rendered as a native masking policy (Snowflake) / column mask (Databricks), `pgcrypto`/KMS at rest |
| Backups | `shop_oltp`: base backup + continuous WAL archiving (PITR). Metadata DBs: dumps. Automated restore drill (P9) | pgBackRest: full + incremental backups and continuous WAL archiving to an object store (see DR plan) |
| High availability | Single node | Primary + streaming replica (Patroni) or a managed service (RDS, Cloud SQL, Azure Database) |
| Logging | `log_connections`, `log_disconnections`, `log_statement=ddl` | Same, plus `pgaudit` (`ddl, role`), shipped to central logging |

### Apache Airflow 3: orchestration

| Setting | Local | Production |
|---|---|---|
| Metadata database | Postgres | Dedicated Postgres, backed up. It holds run state and data intervals ([ADR-0006](adr/0006-pipeline-state.md)) |
| Auth manager | Simple auth manager (development only) | FAB auth manager with OIDC/OAuth SSO, roles per domain team |
| `fernet_key` | From `.env` | From secrets manager. It encrypts connection passwords in the metadata DB and is rotated with `airflow rotate-fernet-key` |
| API / JWT secrets | From `.env` | Long random values from secrets manager, identical across all API server instances |
| Connections & variables | Environment variables | Secrets backend (`[secrets] backend` → Vault, AWS Secrets Manager, GCP, Azure), never stored in the metadata DB or in DAG code |
| Executor | LocalExecutor | KubernetesExecutor or CeleryExecutor |
| Domain isolation | One DAG bundle per domain, domain tasks in their own image | Same, with per-domain images and Kubernetes namespaces/service accounts |
| Components | Single replica each | API server and scheduler with ≥2 replicas, separate DAG processor and triggerer |
| Retries & alerts | Basic | Retries per task, failure and SLA-miss callbacks posting to the on-call channel |

### DataHub: catalog

| Setting | Local | Production |
|---|---|---|
| Default `datahub` user | Present | Removed or password changed, SSO enabled |
| Metadata service auth | Off | `METADATA_SERVICE_AUTH_ENABLED=true`. Ingestion uses personal access tokens scoped to a service account |
| SSO | Off | OIDC (`AUTH_OIDC_ENABLED=true` and related settings) |
| Authorization | Default | Policies per domain: domain owners edit their own assets only |
| Storage | Quickstart containers | Managed or HA MySQL/Postgres, Elasticsearch/OpenSearch and Kafka |
| Metadata as code | Recipes in repo | Same. Domains, glossary and data products are re-applied from git on deploy |

### Apache Superset: reporting

| Setting | Local | Production |
|---|---|---|
| `SECRET_KEY` | From `.env` | Long random value from secrets manager. Superset refuses to start with the default |
| Admin user | Created at init | Replaced by SSO, no shared admin account |
| Database connection | Read-only role | Read-only role per consumer, `SELECT` on data product schemas only |
| Row-level security | Off | On where needed (e.g. sellers see their own data) |
| Dashboards | Exported as code | Same. Dashboards are versioned in `reporting/` and deployed from git |
| Metadata database | Postgres | Dedicated Postgres, backed up |

### CI/CD: GitHub Actions

| Setting | Production |
|---|---|
| Branch protection | `main` protected: PR + review + passing contract checks required |
| CODEOWNERS | Each `domains/<domain>/` owned by its team, `governance/` owned by the governance guild |
| Secrets | GitHub environments with required reviewers, OIDC federation to cloud instead of long-lived keys |
| Action pinning | Third-party actions pinned to a commit SHA |
| Dependency scanning | Dependabot (or Renovate) for Python, Docker images and actions |

### AI agent

| Setting | Production |
|---|---|
| Data access | Through the governed semantic MCP + DataHub MCP only: output ports via the masking views, no raw/staging access (ADR-0020/0021) |
| Credentials | Dedicated read-only Postgres role (`analytics_reader`) and a scoped DataHub token |
| PII | The agent's role sees PII masked (role-based masking, ADR-0013). It holds no unmask role |
| Logging | Every tool call logged `{agent, tool, allowed/refused, sql}` (ADR-0021) |

---

## 3. Go-live checklist

Before promoting to production, every row in section 1 and every "Production" column above
must be met. Three gates are executable:

- [ ] `DGP_ENV=prod make preflight` passes against the production configuration (X2): no default,
      placeholder or weak credential, and no secret hard-coded in a deployed manifest.
- [ ] `make check` is green in CI on the release commit (governance + pinned images/actions X8 + tests).
- [ ] `make prove-p13` passes against the production stack: TLS-only, least privilege, audit logging,
      encryption at rest, gates.
- [ ] Every desired control in §5 (D1 to D10) passes its acceptance test in the target environment.
- [ ] The latest **restore drill** passed within RTO (`s3://<backup bucket>/drills/<latest>.json`
      shows `"passed": true`), run against the production backups (P9, [DR plan](disaster-recovery.md)).

---

## 4. Reference production configuration

Configuration for production. Where a control is already enforced locally (✅), this section
notes what changes for production. The 📄 parts are listed as desired controls in §5. Values in
`<…>` come from the secrets manager or your environment. The platform code needs no change, because
every component already reads its endpoint and credentials from the environment (ADR-0009).

### 4.1 TLS (X3)

**Reverse proxy (Traefik)** terminates TLS for every UI and the remote MCP endpoint. It is the only
public entry point:

```yaml
# traefik static config
entryPoints:
  web:       { address: ":80", http: { redirections: { entryPoint: { to: websecure, scheme: https } } } }
  websecure: { address: ":443", http: { tls: { certResolver: le } } }
certificatesResolvers:
  le: { acme: { email: <ops-email>, storage: /acme/acme.json, tlsChallenge: {} } }
# per-service routers: airflow.<domain>, datahub.<domain>, superset.<domain>, mcp.<domain>
```

Set HSTS and secure cookies in each app: Superset `TALISMAN_ENABLED = True`,
`SESSION_COOKIE_SECURE = True`, and the DataHub frontend behind HTTPS with `AUTH_COOKIE_SECURE=true`.

**Postgres** (all instances): **implemented locally**. `platform/core/infra/postgres/hardened-entrypoint.sh`
starts every instance with `ssl=on`, `ssl_min_protocol_version=TLSv1.2` and
`hba_file=…/pg_hba.conf` (`hostssl` + scram, `hostnossl … reject`). Clients use libpq's
`PGSSLMODE=verify-full` + `PGSSLROOTCERT` (Make exports them for host tools, and the Airflow image sets
them). For production, swap the certificate source (your CA or cert-manager instead of
`make certs`) and narrow the `hostssl` address to the platform subnets.

**MinIO**: mount `public.crt`/`private.key` under `--certs-dir` and set `MINIO_ENDPOINT=https://…`.
Clients verify against the CA, and the s3-browser is not deployed in production.

### 4.2 Single sign-on (X4)

| Component | Configuration |
|---|---|
| Airflow 3 | `AIRFLOW__CORE__AUTH_MANAGER=airflow.providers.fab.auth_manager.fab_auth_manager.FabAuthManager`. `webserver_config.py`: `AUTH_TYPE = AUTH_OAUTH`, `OAUTH_PROVIDERS = [<oidc provider>]`, `AUTH_ROLES_MAPPING` from IdP groups → one role per domain team (each sees only its own DAG bundle) |
| DataHub | `AUTH_OIDC_ENABLED=true`, `AUTH_OIDC_CLIENT_ID`, `AUTH_OIDC_CLIENT_SECRET`, `AUTH_OIDC_DISCOVERY_URI`, `AUTH_OIDC_BASE_URL`, `METADATA_SERVICE_AUTH_ENABLED=true`. Default `datahub` user removed. Ingestion with a service-account access token |
| Superset | `superset_config.py`: `AUTH_TYPE = AUTH_OAUTH`, `OAUTH_PROVIDERS` (OIDC), `AUTH_ROLES_MAPPING`. No local admin account |
| Remote MCP (`ai-mcp-http`) | Behind the proxy with OAuth 2.1 per the MCP authorization spec (the IdP issues tokens and the proxy validates them before the request reaches the server). stdio clients stay local-only |

### 4.3 Secrets manager and least privilege (X1, X5)

- **Runtime injection**: External Secrets Operator (Kubernetes) or the platform's native secret
  binding syncs the secrets manager into environment variables. They use the same names as
  `.env.example`, so no code changes. `DGP_ENV=prod make preflight` runs as a deploy step and fails
  the rollout on any default.
- **Airflow** connections/variables come from a secrets backend, never the metadata DB:
  `AIRFLOW__SECRETS__BACKEND=airflow.providers.hashicorp.secrets.vault.VaultBackend` (or
  `airflow.providers.amazon.aws.secrets.secrets_manager.SecretsManagerBackend`) with
  `AIRFLOW__SECRETS__BACKEND_KWARGS` pointing at the mount/paths.
- **Rotation**: per-domain role passwords and the Fernet key rotate on a schedule
  (`airflow rotate-fernet-key`, and Postgres `ALTER ROLE … PASSWORD` from the manager's rotation hook).
- **Admin plane: implemented locally.** `access_admin` (a member of each domain role, `CREATE` on the
  database, not a superuser) applies masking views and grants and owns the `<domain>_masked` schemas.
  `catalog_reader` (`pg_read_all_data`) feeds DataHub. Both are created by the idempotent
  `10-hardening-roles.sh` (`make db-roles` on a running platform). For production, `compliance`
  becomes a `NOLOGIN` group granted to named users.
- **Per-domain AI readers: implemented locally.** Each federated agent connects as `agent_<domain>`,
  granted only its domain's ports (masked where PII). P11 asserts the warehouse denies it every other
  domain's port.

### 4.4 Network isolation (X6)

- Two networks: `edge` (reverse proxy only) and `internal` (`internal: true` in Compose, or a
  default-deny `NetworkPolicy` in Kubernetes). Postgres, MinIO API, Kafka, OpenSearch and MySQL have
  **no published ports** and are reachable only from the services that need them.
- Allow-list per flow, for example: Airflow workers → Postgres/MinIO, DataHub GMS → MySQL/OpenSearch/Kafka,
  Superset → analytics (read-only role), MCP servers → analytics + DataHub GMS.
- Partners write to `landing-inbox/<source>/` through a dedicated endpoint with per-partner write-only
  credentials (generated policies, ADR-0024, and the event trigger, ADR-0016).

### 4.5 Encryption at rest (X7)

- **Implemented locally:** MinIO SSE-S3 is the default on every bucket, under the built-in KMS with a
  static key (`MINIO_KMS_SECRET_KEY`). `make encrypt-at-rest` migrates pre-existing objects, and P13
  asserts that every current object is encrypted. Production replaces the static key with KES.
- Volumes on encrypted disks (LUKS / cloud EBS-PD-managed-disk encryption with a customer key).
- MinIO: KES with an external KMS (`MINIO_KMS_KES_ENDPOINT`, `MINIO_KMS_KES_KEY_NAME`) and
  `mc encrypt set sse-kms <key> <alias>/<bucket>` on every bucket. `landing-archive` is created
  `--with-lock` with NO default retention (the loader locks accepted objects, and quarantine must expire).
  The backup bucket is created `--with-lock` with `mc retention set --default GOVERNANCE <days>`.
- Backups: pgBackRest `repo1-cipher-type=aes-256-cbc` with the passphrase in the secrets manager.
- PII: role-based masking at read (ADR-0013). Optionally `pgcrypto`/column encryption for the most
  sensitive fields.

### 4.6 High availability, logging, monitoring (HA, X9, X10)

| Component | HA configuration |
|---|---|
| Postgres (`shop_oltp`, `analytics`, metadata DBs) | Patroni primary + synchronous replica, or a managed service (RDS / Cloud SQL / Azure Database) with multi-AZ |
| MinIO | Distributed mode, ≥ 4 nodes with erasure coding, site replication to a second location (DR) |
| Airflow | ≥ 2 schedulers and API servers (active/active via DB row locks), separate DAG processor + triggerer, KubernetesExecutor |
| DataHub | ≥ 2 GMS and frontend replicas, OpenSearch 3 nodes, Kafka 3 brokers with replication factor 3 |
| Superset | Stateless replicas behind the proxy, Redis for cache/results, Postgres metadata DB |
| MCP servers | Stateless (streamable-HTTP), ≥ 2 replicas behind the proxy |

Logging: `log_connections`/`log_disconnections`/`log_statement=ddl` are **on locally** (P13). For
production, add `pgaudit` (`pgaudit.log = 'ddl, role'`), the MinIO audit webhook, and DataHub and
Superset access logs, and ship all of them plus the AI audit log `{agent, tool, allowed/refused, sql}`
to the central log store (Loki/ELK/cloud logging).

Monitoring: the DR alert conditions are **executable locally** (`make dr-status`: backup older than
26 h, or no passing drill in 7 days). For production, Prometheus scrapes Postgres/MinIO/Airflow
(StatsD/OTel) metrics, `dr-status` runs on a schedule, and alerts fire on failed task runs, failed
contract checks/quarantines, SLA misses (ADR-0011), disk > 80 %, WAL-archive failures
(`pg_stat_archiver.failed_count`), replication lag > RPO and a failing `dr-status`.

---

## 5. Desired production controls (not testable locally)

These controls are **specified, not proven**. Each needs infrastructure a single laptop cannot
faithfully provide (an identity provider, public DNS and certificates, multiple hosts or regions, a
cloud KMS, a paging system). They are requirements for the production deployment. Each lists the
acceptance test the deployment must pass, so it becomes testable once that infrastructure exists.

| # | Desired control | Why not tested locally | Acceptance test in the target environment | Config |
|---|---|---|---|---|
| D1 | **TLS reverse proxy** for every UI, MinIO and remote MCP, HSTS, secure cookies | Needs real DNS names + publicly trusted certs. Local UIs stay plain HTTP on loopback for dev ergonomics | `curl -I http://<ui>` → 301 to HTTPS, `testssl.sh <host>` grade A, no service port reachable except 443 | §4.1 |
| D2 | **SSO (OIDC)** for Airflow, DataHub, Superset, OAuth 2.1 on remote MCP, IdP groups → per-domain roles | Needs an identity provider and real user directory | Unauthenticated request → IdP redirect. A sales user cannot edit customer DAGs/assets. MCP call without token → 401 | §4.2 |
| D3 | **Secrets manager** injection + rotation, Airflow secrets backend | Needs Vault / a cloud secrets service | `DGP_ENV=prod make preflight` passes with injected values. Rotating a role password needs no redeploy. No secret in images, manifests or the Airflow metadata DB | §4.3 |
| D4 | **Network isolation** (edge/internal networks, default-deny policies, per-flow allow-lists) | Needs a multi-host/Kubernetes network to be meaningful | Port scan from outside shows only 443. A pod outside the allow-list cannot open Postgres/MinIO | §4.4 |
| D5 | **External KMS (KES)**, encrypted volumes, pgBackRest cipher, object lock + cross-region replication | Needs a cloud KMS / second site | Key revocation makes objects unreadable. An object-locked backup cannot be deleted inside retention. The replica bucket has the latest backup | §4.5 |
| D6 | **High availability** for every stateful service | Needs multiple nodes/zones | Kill the Postgres primary → failover within the RTO, no committed data loss. Kill one scheduler/GMS replica → no outage | §4.6 |
| D7 | **Central logging + `pgaudit`** | Needs a log platform | A role change in Postgres appears in the central store within a minute, attributed to a user | §4.6 |
| D8 | **Alert routing + scheduled DR checks** (`dr-status`, drill weekly from CI against production backups) | Needs a scheduler with platform access and a paging channel | Stopping backups pages on-call within 26 h. A failing drill pages immediately | §4.6, DR §5 |
| D9 | **DataHub search-index rebuild** after a metadata restore | Needs the full GMS stack inside the drill sandbox | After restore + `restore-indices`, search returns the restored assets | DR §4 |
| D10 | **Production-scale drill** | RTO/RPO measured locally are Olist-scale (seconds) | Drill against production-sized backups meets every RTO in DR §1 | DR §5 |
| D11 | **Landing bucket controls** (ADR-0016): TLS-only, SSE-KMS with each partner's own key, storage-class transitions | The local object store is plain HTTP with one built-in key and no remote storage tier | Apply `platform/access/landing/production/*` (fill `${AWS_ACCOUNT_ID}`/`${AWS_REGION}`/key ids). An `http://` request → 403. An upload without SSE-KMS or with another partner's key → 403. A 365-day accepted object shows `GLACIER_IR` after 90 days | ADR-0016 §5 |
| D12 | **Partner credentials from the secrets manager** (ADR-0016) | Needs a secrets manager and a partner onboarding channel | Every principal in `production/secrets.json` exists at its path with ≤ 90-day rotation. No `LANDING_*_SECRET` in any env file or image. A rotated partner secret works without a platform redeploy | ADR-0016 §5, D3 |
