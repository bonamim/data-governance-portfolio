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
| **DataHub** (catalog) | http://localhost:9002 | *set at M4* | *set at M4* | *planned — M4* |
| **Superset** (BI) | http://localhost:8088 | *set at M6* | *set at M6* | *planned — M6* |

## Databases & APIs

| Service | Host:Port | DB | User | Password (env var) |
|---|---|---|---|---|
| **MinIO S3 API** | http://localhost:9000 | — | `MINIO_ROOT_USER` | `MINIO_ROOT_PASSWORD` |
| **Postgres — shop_oltp** (operational source) | localhost:5432 | `shop_oltp` | `OLTP_SUPERUSER` (postgres) | `OLTP_SUPERUSER_PASSWORD` |
| **Postgres — analytics** (warehouse) | localhost:5433 | `analytics` | `ANALYTICS_SUPERUSER` (postgres) | `ANALYTICS_SUPERUSER_PASSWORD` |

Least-privilege roles on the warehouse (passwords in `.env`): `platform_loader`,
`sales_role`, `customer_role`, `marketplace_role`, `customer_experience_role`,
`reference_role`. dbt connects as the per-domain role (see `platform/transform/profiles.yml`).

> The Airflow metadata Postgres has no host port (internal only). The `datahub` and
> `superset` rows are placeholders until those milestones land.
>
> MinIO's community server dropped its built-in web console in 2025 ([ADR-0007](adr/0007-minio-image-source.md)),
> so the **S3 browser** (a separate container pointed at the S3 API) stands in for it on
> the port the console used to occupy.
