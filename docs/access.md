# Local access & endpoints

URLs and credentials for the local stack. **Local dev values only.** None are
production-safe (see [production-readiness.md](production-readiness.md): SSO/OIDC,
secrets manager, TLS). Credentials come from `.env`, and the defaults below are the ones
in `.env.example`. Run `make endpoints` to print the live values from your `.env`.

## UIs

| Service | URL | User | Password (env var) | Default |
|---|---|---|---|---|
| **S3 browser** (object storage UI) | http://localhost:9001 | | | **No login** (reads MinIO with the root creds, server-side) |
| **Airflow** (orchestration) | http://localhost:8080 | | | **No login** (dev SimpleAuthManager, all admins) |
| **DataHub** (catalog) | http://localhost:9002 | | | **No login hardening yet** (dev `datahub` / `datahub`) |
| **Superset** (BI) | http://localhost:8088 | | | **No SSO yet** (dev `admin` / `admin`) |
| **pgAdmin** (database UI, `make pgadmin`) | http://localhost:5050 | | | **No login** (desktop mode). Servers preloaded (`verify-full` TLS), passwords injected from `.env` |
| **Ossie Visualizer** (the ontology as a graph, `make ossie-ui`) | http://127.0.0.1:4178 | | | **No login**, loopback only. *Import document* → `build/ossie/commerce.ontology.yaml` |

**pgAdmin** opens with a `governance-platform` group holding every platform Postgres, already
authenticated: `shop_oltp` (superuser), `analytics` (superuser), `analytics` **as
`analytics_reader`** (exactly what BI/AI consumers can see: output ports only, PII masked, useful
for checking governance by hand) and the Airflow metadata DB. pgAdmin speaks Postgres only, so it
does not cover DataHub's metadata store (MySQL, reachable on `localhost:3306` with `datahub` /
`datahub` from any MySQL client) or Superset's metadata (SQLite inside the `dgp-bi` container). Both
of those are dev-only stores that production replaces with managed databases.

## Databases & APIs

| Service | Host:Port | DB | User | Password (env var) |
|---|---|---|---|---|
| **MinIO S3 API** | http://localhost:9000 | | `MINIO_ROOT_USER` | `MINIO_ROOT_PASSWORD` |
| MinIO landing loader (what ingestion runs as) | http://localhost:9000 | | `LANDING_LOADER_USER` (landing-loader) | `LANDING_LOADER_SECRET_KEY` |
| MinIO partner `olist` (write-only, PoC simulator) | http://localhost:9000 | | `partner-olist` | `LANDING_PARTNER_OLIST_SECRET` |
| **DataHub GMS API** | http://localhost:8081 | | | dev: no token (ingestion/CLI target) |
| **Semantic MCP server**, remote (`make ai-mcp-http`) | http://127.0.0.1:8900/mcp | | | No auth, loopback only. Local clients run it over stdio instead (`make ai-mcp`), with no URL. See [ai/README.md](../ai/README.md) |
| **Postgres shop_oltp** (operational source) | localhost:5432 | `shop_oltp` | `OLTP_SUPERUSER` (postgres) | `OLTP_SUPERUSER_PASSWORD` |
| **Postgres analytics** (warehouse) | localhost:5433 | `analytics` | `ANALYTICS_SUPERUSER` (postgres) | `ANALYTICS_SUPERUSER_PASSWORD` |
| Restore-drill sandbox, while `make restore-drill` / `make prove-p9` runs | 127.0.0.1:55432 (OLTP), 127.0.0.1:55433 (analytics) | as above | as above | Restored from the backups, removed after the drill |
| DataHub's own stores (dev quickstart) | MySQL localhost:3306, OpenSearch http://localhost:9200, Kafka localhost:9092 | | `datahub` (MySQL) | `datahub` (MySQL). Internal to the catalog, never a data path |

Least-privilege roles on the warehouse (passwords in `.env`): `platform_loader`,
`sales_role`, `customer_role`, `marketplace_role`, `customer_experience_role`,
`reference_role`, the read-only consumer `analytics_reader`, the unmask role `compliance`, one
`agent_<domain>` per AI agent, `access_admin` (applies masking + grants) and `catalog_reader`
(DataHub ingestion). dbt connects as the per-domain role (see `platform/core/transform/profiles.yml`).

## Landing zone: where to look (ADR-0024)

| What | Where |
|---|---|
| A partner's drop, not yet processed | S3 browser → `landing-inbox/<source>/<dataset>/` (empty once the loader has run) |
| Accepted files (immutable, loaded) | `landing-archive/accepted/<source>/<dataset>/<partition>/` |
| Rejected files + why | `landing-archive/quarantine/<source>/<dataset>/<partition>/<run_id>/`: the file and `_reason.json` |
| Every delivery's outcome + per-rule results | `platform_meta.ingestion_ledger` (pgAdmin → analytics): `status`, `partition`, `archive_key`, `checks` |
| The same results in the catalog | published automatically after each Airflow ingestion run (`publish_quality` task, or by hand with `make catalog-quality`), then DataHub → search `olist/reviews (landing)` → **Quality** tab (one assertion per contract rule, runs per partition) |
| Who may touch what | `platform/access/landing/` (generated from `sources.yml` by `make landing-access`) |

Try it: `make prove-p14` runs the whole path (bad data, bad file name, good file, resend, access
checks, DataHub). Policies and principals are applied by `make up`.

> **TLS only.** Postgres refuses plain TCP. A client on the host needs `sslmode=require` at least, or
> better `sslmode=verify-full sslrootcert=platform/core/infra/tls/out/ca.crt` (Make exports `PGSSLMODE` /
> `PGSSLROOTCERT` for every target, and you run `make certs` once). `make psql-oltp` / `make psql-analytics`
> use the in-container socket and need nothing.

> The Airflow metadata Postgres has no host port (internal only). pgAdmin reaches it on the
> platform network.
>
> MinIO's community server dropped its built-in web console in 2025 ([ADR-0007](adr/0007-minio-image-source.md)),
> so the **S3 browser** (a separate container pointed at the S3 API) stands in for it on
> the port the console used to occupy.
