# Disaster Recovery Plan

How the platform's data and metadata are protected, how they are restored, and how we
prove that restores work.

---

## 1. Recovery objectives

| Tier | Component | RPO (max data loss) | RTO (max downtime) |
|---|---|---|---|
| **1 — Critical** | Landing zone (MinIO) — raw files are the source of truth for files-based domains | 0 (replicated, versioned, immutable) | 4 h |
| **1 — Critical** | `shop_oltp` (stands in for an operational system) | 5 min | 1 h |
| **2 — Important** | `analytics` warehouse | 24 h *(rebuildable from sources)* | 8 h |
| **2 — Important** | DataHub metadata (manual edits: docs, ownership changes, comments) | 24 h | 8 h |
| **2 — Important** | Airflow metadata DB (run state, data intervals), ingestion ledger, `platform-state` bucket (dbt artifacts) | 24 h | 4 h |
| **3 — Rebuildable** | Superset metadata, Elasticsearch indices, Kafka | 24 h | 24 h |

Targets are a starting point; adjust them to the business before go-live.

---

## 2. What is rebuildable vs. what must be backed up

The key design rule: **anything that can be regenerated from git or from the sources is
regenerated, not restored.** This keeps backups small and restores simple.

| Asset | Recovery method |
|---|---|
| Contracts, dbt projects, governance, dashboards, ingestion recipes | **Git** — redeploy |
| Raw files in the landing zone | **Backup + replication** — cannot be regenerated |
| `shop_oltp` | **Backup** (PITR) — cannot be regenerated |
| `analytics` warehouse | **Rebuild** — reload raw from sources + `dbt build`; backup kept to shorten RTO |
| DataHub metadata store (MySQL/Postgres) | **Backup** — holds manual edits not in git |
| DataHub search indices (Elasticsearch) | **Rebuild** from the metadata store with DataHub's restore-indices job |
| DataHub Kafka | **Not backed up** — transient event log |
| Airflow metadata DB | **Backup** — run history and data intervals ([ADR-0006](adr/0006-pipeline-state.md)) |
| Ingestion ledger (`platform_meta` in `analytics`) | **Backup** with `analytics`; rebuilt if the warehouse is rebuilt from scratch |
| `platform-state` bucket (dbt manifests) | **Versioning + replication**, like the landing zone |
| Superset metadata DB | **Backup** (small); dashboards also in git |
| Secrets | Held in the secrets manager, which has its own backup/replication |

---

## 3. Backup configuration

### MinIO
- **Versioning** on all buckets — protects against overwrite and accidental delete.
- **Object lock** (governance mode) on `landing` with a retention period — protects against
  ransomware and malicious delete.
- **Replication** to a second MinIO site or a cloud bucket in another region.

### PostgreSQL (`shop_oltp`, `analytics`, DataHub, Airflow, Superset metadata)
- **pgBackRest** with its repository in a dedicated, versioned, object-locked bucket —
  a different location from the landing zone.
- Weekly full backup, daily differential, **continuous WAL archiving** (enables
  point-in-time recovery, giving the 5-minute RPO on `shop_oltp`).
- Backups encrypted (`repo1-cipher-type=aes-256-cbc`) with the key held in the secrets manager.
- Retention: 4 weekly fulls; WAL kept for the same window.

### Monitoring
- Alert when a backup fails or when the latest successful backup is older than its schedule.
- Alert when replication lag exceeds the RPO.

---

## 4. Disaster scenarios and runbooks

| Scenario | Response |
|---|---|
| A landing file is overwritten or deleted | Restore the previous object version in MinIO |
| A bad load or bad dbt run corrupts the warehouse | Fix the cause; clear and re-run the affected Airflow tasks for the bad intervals (idempotent loads + `dbt build`) |
| A table is dropped in `shop_oltp` | Point-in-time restore to a new instance before the drop; copy the table back |
| `analytics` server is lost | Provision a new instance; restore latest backup *or* rebuild from sources; run `dbt build` |
| DataHub is lost | Restore metadata DB; run restore-indices; re-apply metadata as code from git |
| Ransomware / malicious delete | Object-locked backups and landing files cannot be deleted inside retention; restore from them |
| Full site loss | Redeploy platform from git in the second location; restore from replicated backups in tier order |

Detailed step-by-step runbooks are added to `docs/runbooks/` with each component.

---

## 5. Restore drills

A backup is only trusted once it has been restored. The repository includes an
automated **restore drill** (milestone M8) that:

1. Restores the latest `shop_oltp` and DataHub backups into fresh, isolated containers.
2. Rebuilds `analytics` from the landing zone and the restored `shop_oltp`.
3. Runs every contract quality check against the restored data.
4. Reports the measured recovery time against the RTO.

The drill runs on a schedule, and its result is a go-live requirement in
[Production readiness](production-readiness.md).
