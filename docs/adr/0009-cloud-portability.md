# ADR-0009: Cloud-portable by design

**Status:** Accepted

## Context
The platform must run locally with one `make up` (reproducible by anyone, no cloud
account), yet be promotable to a cloud without a rewrite. If local and cloud diverge into
two codebases, the "promotable, not just demoed" promise breaks.

## Decision
Keep every component behind a **standard interface with a configurable endpoint**, so moving
to a cloud is a configuration change, not a code change.

| Local | Cloud | What changes |
|---|---|---|
| MinIO (S3 API) | AWS S3 / GCS (S3 mode) / Azure Blob (S3 proxy) | `MINIO_ENDPOINT` unset → real S3, credentials from the instance role |
| Postgres in Docker | RDS / Cloud SQL / Azure Database | connection host/port/password only |
| Airflow via Docker Compose | MWAA / Astronomer / Helm on Kubernetes | deployment target, DAGs and platform code unchanged (ADR-0005) |
| `.env` file | Vault / AWS/GCP/Azure secrets manager | `settings.py` reads the environment, and a secrets backend populates it |

The object-store client is the concrete seam: with `MINIO_ENDPOINT` set it uses MinIO with
path-style addressing. Unset, the same code talks to AWS S3 and takes credentials from
boto3's default chain (IAM role, instance profile, environment). No `if cloud:` branches.

## What this explicitly is not
Not a managed-service deployment in this repo, and **not** anything that depends on a free
tier. Managed Airflow (MWAA) has no free tier, and free-tier compute cannot run the full
stack (DataHub alone needs several GB). Depending on the maintainer's cloud account would
also break reproducibility for anyone else. The cloud path is **documented and config-driven**
(see [production-readiness.md](../production-readiness.md)), and the runnable artefact stays local.

## Consequences
- The platform runs identically on a laptop and against real S3. Only the environment differs.
- Portability is a standing constraint on every new component: it must sit behind a standard
  protocol (S3, SQL, OpenLineage, OIDC) with the endpoint in configuration.
- Avoiding managed/free-tier lock-in keeps the repo reproducible and the costs honest.
