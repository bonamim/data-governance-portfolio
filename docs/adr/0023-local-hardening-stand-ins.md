# ADR-0023: Prove hardening locally where possible, specify the rest as desired controls

**Status:** Accepted (M8, proof P13)

## Context
[Production readiness](../production-readiness.md) lists eleven cross-cutting controls (X1–X11).
Documenting a control is not the same as proving it. A portfolio that claims "production-ready"
should *demonstrate* every control that a single machine can faithfully host, and be explicit about
the ones it cannot.

## Decision
**Enforce and prove locally** every control whose behaviour is the same on a laptop as in
production. Run them on the real services, not on mocks:

| Control | Local implementation | Proven by |
|---|---|---|
| X2 no default credentials | `platform/core/infra/preflight.py`; `DGP_ENV=prod` refuses to start | unit tests, P13 |
| X3 TLS (Postgres) | `hardened-entrypoint.sh`: `ssl=on`, TLS ≥ 1.2, `hostssl` + scram, `hostnossl reject`; local CA (`make certs`); clients `verify-full` | P13 (plain TCP refused, verify-full OK, all sessions encrypted) |
| X5 least privilege | `access_admin` (masking + grants), `catalog_reader` (DataHub), `agent_<domain>` per AI agent; no automation uses `postgres` | P8, P11 (warehouse-level agent isolation), P13 |
| X7 encryption at rest (objects) | MinIO SSE-S3 default on every bucket (built-in KMS, static key); existing objects migrated | P13 |
| X8 pinned versions | images by digest, GitHub Actions by commit SHA | `check-pinned-images` in CI |
| X9 audit logging (Postgres) | `log_connections`, `log_disconnections`, `log_statement=ddl` | P13 (finds its own connection in the log) |
| X10 DR alert conditions | `make dr-status` (stale backup / no recent passing drill) | P13 |
| X11 backups + RPO/RTO | base backup + WAL archiving + PITR drill | P9 |

**Specify as desired controls** (production-readiness §5, D1–D10) the ones that need infrastructure a
laptop cannot provide faithfully: a TLS reverse proxy with real DNS/certs, SSO via an identity
provider, a secrets manager, multi-host network isolation, an external KMS with object lock and
replication, HA, central logging, alert routing, the DataHub index rebuild in the drill, and a
production-scale drill. Each has an acceptance test for the target environment and is a go-live gate.

## Consequences
- The local stack is closer to production: Postgres refuses plain TCP, objects are encrypted, and
  automation never holds superuser.
- Dev ergonomics stay: UIs remain plain HTTP on loopback with no login (D1/D2 are production-only),
  and operators keep socket-only break-glass access inside the containers.
- The work found and fixed two real defects. First, `publish.py` dropped the masking view (ADR-0022).
  Second, every raw load cascade-dropped the view-materialized output ports. A third latent bug was
  also fixed: the analytics container lacked the env vars `00-init.sh` needs, so a fresh `make up`
  failed at init.
- Dependencies: `make up` now requires `make certs` (automatic) and the MinIO KMS key in `.env`.
