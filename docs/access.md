# Local access & endpoints

URLs and credentials for the local stack. **Local dev values only** — none are
production-safe (see [production-readiness.md](production-readiness.md): SSO/OIDC,
secrets manager, TLS). Credentials come from `.env`; the defaults below are the ones
in `.env.example`. Run `make endpoints` to print the live values from your `.env`.

## UIs

| Service | URL | User | Password (env var) | Default |
|---|---|---|---|---|
| **S3 browser** (object storage UI) | http://localhost:9001 | — | — | **No login** (reads MinIO with the root creds, server-side) |
| **Airflow** (orchestration) | http://localhost:8080 | — | — | **No login** (dev SimpleAuthManager, all admins) |
| **DataHub** (catalog) | http://localhost:9002 | — | — | **No login hardening yet** (dev `datahub` / `datahub`) |
| **Superset** (BI) | http://localhost:8088 | — | — | **No SSO yet** (dev `admin` / `admin`) |

## Databases & APIs

| Service | Host:Port | DB | User | Password (env var) |
|---|---|---|---|---|
| **MinIO S3 API** | http://localhost:9000 | — | `MINIO_ROOT_USER` | `MINIO_ROOT_PASSWORD` |
| **DataHub GMS API** | http://localhost:8081 | — | — | dev: no token (ingestion/CLI target) |
| **Postgres — shop_oltp** (operational source) | localhost:5432 | `shop_oltp` | `OLTP_SUPERUSER` (postgres) | `OLTP_SUPERUSER_PASSWORD` |
| **Postgres — analytics** (warehouse) | localhost:5433 | `analytics` | `ANALYTICS_SUPERUSER` (postgres) | `ANALYTICS_SUPERUSER_PASSWORD` |

Least-privilege roles on the warehouse (passwords in `.env`): `platform_loader`,
`sales_role`, `customer_role`, `marketplace_role`, `customer_experience_role`,
`reference_role`, the read-only consumer `analytics_reader`, the unmask role `compliance`, one
`agent_<domain>` per AI agent, `access_admin` (applies masking + grants) and `catalog_reader`
(DataHub ingestion). dbt connects as the per-domain role (see `platform/transform/profiles.yml`).

> **TLS only.** Postgres refuses plain TCP. A client on the host needs `sslmode=require` at least, or
> better `sslmode=verify-full sslrootcert=platform/infra/tls/out/ca.crt` (Make exports `PGSSLMODE` /
> `PGSSLROOTCERT` for every target; run `make certs` once). `make psql-oltp` / `make psql-analytics`
> use the in-container socket and need nothing.

> The Airflow metadata Postgres has no host port (internal only).
>
> MinIO's community server dropped its built-in web console in 2025 ([ADR-0007](adr/0007-minio-image-source.md)),
> so the **S3 browser** (a separate container pointed at the S3 API) stands in for it on
> the port the console used to occupy.
