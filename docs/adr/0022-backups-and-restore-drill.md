# ADR-0022: Backups and the automated restore drill (proof P9)

**Status:** Accepted (implemented locally in M8 — `platform/core/recovery/`, `make prove-p9`)

## Context
[Disaster recovery](../disaster-recovery.md) sets per-tier RPO/RTO targets and one rule: **anything
that can be regenerated from git or the sources is regenerated, not restored**. A backup nobody has
restored is a hope, not a control, so go-live requires a restore drill that has passed
([production-readiness](../production-readiness.md) §3). We need backups of exactly the stores that
cannot be regenerated, and a repeatable drill that proves them and measures recovery time, running
on the laptop stack with no cloud account (ADR-0009).

## Decision

### 1. Back up only what can't be regenerated
| Store | Method | Why |
|---|---|---|
| `shop_oltp` | physical base backup (`pg_basebackup`) + **continuous WAL archiving** → point-in-time recovery | operational source of truth (tier 1, RPO 5 min) |
| ingestion ledger (`platform_meta`) | backed up | pipeline state (ADR-0006) |
| Airflow metadata DB | backed up | run history, data intervals |
| DataHub metadata store (MySQL) | backed up (`mysqldump --single-transaction`) | manual catalog edits not in git |
| Superset metadata (SQLite) | backed up (online `sqlite3` backup) | tier 3; dashboards also in git |
| `analytics` warehouse | **rebuilt**, not backed up | regenerable from the landing zone + `shop_oltp` + dbt |
| landing zone | protected in place (versioning; prod: object lock + replication) | tier-1 source of truth |

The base backup, together with the WAL archive (`archive_mode=on`, `archive_timeout=60s`, a
dedicated volume standing in for the archive store), gives point-in-time recovery. After each base
backup the archive is pruned to what that base needs (`pg_archivecleanup`).

`make backup` writes `s3://platform-backups/backups/<UTC timestamp>/`: one file per store plus a
`manifest.json` with each file's sha256, row counts of key tables taken **before** the dump, and the
row count of every output port (the baseline a rebuild must reproduce). A store whose service is
down is recorded as `skipped`, never silently dropped. The bucket is versioned with ~4 weekly
cycles kept.

### 2. The drill restores into an isolated sandbox and proves it
`make restore-drill` (`make prove-p9` takes a fresh backup first) starts a separate compose project
(`dgp-drill`, its own network and volumes, loopback-only ports, the **same pinned images**), then:
1. **measures the RPO**: commits a heartbeat on the live `shop_oltp` after the backup and times its
   WAL segment's arrival in the archive;
2. downloads the latest backup and verifies every sha256;
3. **point-in-time recovers** `shop_oltp` (base backup into an empty data dir, `recovery.signal`,
   `restore_command` from the read-only archive) and requires the post-backup heartbeat to be
   present. It restores the other stores from their dumps, and checks each holds **at least** the
   backup-time rows (`>=`, since the live stores keep writing between count and dump);
4. **rebuilds** `analytics` the way a real recovery would: roles/schemas from the git init script,
   raw reloaded from the landing zone and the *restored* `shop_oltp` through the real ingestion gate,
   then every domain published through write-audit-publish, so **every contract quality check runs on
   the recovered data**;
5. checks the rebuilt output ports equal the pre-disaster row counts, and re-runs P8 against the
   sandbox (access control and PII masking survive a rebuild);
6. reports each store's recovery time against its RTO and writes the report to
   `s3://platform-backups/drills/<timestamp>.json`. It exits 0 only if every check passed and every
   RTO was met.

The drill found a real defect on its first run: `publish.py` (also the Airflow transform DAG's
path) re-granted after a rebuild but never re-applied masking. dbt's table swap drops the old table
with `CASCADE`, which takes the masking view with it. Publishing now re-applies masking before
grants.

### 3. Local stand-ins vs production
| Concern | Local (this repo) | Production |
|---|---|---|
| Postgres backups | `shop_oltp`: `pg_basebackup` + WAL archive volume (PITR); metadata DBs: logical dumps | pgBackRest: weekly full + daily diff + continuous WAL archiving to an object store, `repo1-cipher-type=aes-256-cbc` |
| Backup location | `platform-backups` bucket (versioned, SSE) on the same MinIO; WAL on a separate volume | separate account/region, object-locked (governance mode), replicated |
| DataHub | `mysqldump` + row-count check | managed DB snapshots + PITR; after restore run DataHub's `restore-indices` job to rebuild search |
| Schedule | on demand (`make prove-p9`) | backups on the pgBackRest/managed schedule; the drill weekly from CI against a recovery environment, alerting on failure or on a stale last-pass |
| RTO measured | at Olist scale (seconds) | at production scale: the report records the measured time, and the targets in DR §1 are the gate |

The manifest + drill contract is identical in both columns. Only the dump engine and the bucket
location change.

## Consequences
- P9 is reproducible with one command, and its result is stored as evidence for go-live.
- The RPO is proven locally: the measured archive lag of a real committed write (≤ `archive_timeout`,
  58 s on the last run) against the 5-minute target, and PITR replays past the base backup.
- This work surfaced a second defect (the backup's port baseline failed after a live reload): every
  raw load ran `DROP TABLE … CASCADE`, which removed the
  view-materialized output ports, their masking view and grants until the next transform. Raw loads
  now `TRUNCATE` in place when the columns are unchanged (regression test in
  `platform/core/ingestion/tests/test_db_integration.py`).
- The drill reads the live landing zone (tier 1 is protected in place, not copied). It asserts
  versioning is on; object lock and replication are production settings.
