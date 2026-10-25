# ADR-0024: Landing-zone layout, access and lifecycle (inbox + archive)

**Status:** Accepted (§7 and the idempotency consequence amended by [ADR-0025](0025-separate-contract-gate-and-raw-load.md): the gate and the load are separate tasks)
**Amends** [ADR-0011](0011-landing-zone-slas-and-partitioning.md) §2–§3 (partitioning, retention).
**Prepares** [ADR-0016](0016-event-driven-ingestion.md) (event-driven ingestion: the trigger plugs
into the inbox defined here).

## Context
The landing zone was one bucket, `landing/<source>/<dataset>/dt=YYYY-MM-DD/<file>`, with rejected
files *copied* to `landing/quarantine/<key>`. Testing it end to end exposed four problems:

1. **The file stayed where it arrived.** Quarantine was a copy, so every run re-validated and
   re-quarantined the same bad file.
2. **No security boundary.** Partners' drops, accepted evidence and rejected files shared one
   bucket and one root prefix, so "partner may write only their own data" and "nobody may delete
   accepted evidence" could not be expressed as policy.
3. **One retention for everything.** A single 365-day rule covered accepted files, rejected
   files and noise alike.
4. **The partner had to adopt our layout.** `dt=YYYY-MM-DD/` in the arrival path is our
   convention. A partner must not be forced to change their process.

The landing gate's results also lived only in the ingestion ledger; DataHub never saw them.

## Decision

### 1. Two buckets, shared by every source
Lifecycle rules, IAM policies, notifications, object lock and encryption are all configured per
bucket and matched by key prefix. Splitting by **lifecycle stage** (not by source) keeps the number
of rules fixed however many sources are onboarded, and keeps onboarding a config change.

```
s3://<env>-landing-inbox/                          partner-facing, transient
  <source>/<dataset>/<whatever the partner sends>     partner: PutObject on <source>/* only
                                                      loader:  read + delete
                                                      lifecycle: 30d safety-net expiry, 7d old versions

s3://<env>-landing-archive/                        platform-owned, object lock enabled
  accepted/<source>/<dataset>/<partition>/<file>      immutable evidence + replay source
                                                      per-object lock until retentionDays;
                                                      lifecycle by tag retention-class=<N>d
  quarantine/<source>/<dataset>/<partition>/<run_id>/<file>
  quarantine/<source>/<dataset>/<partition>/<run_id>/_reason.json
                                                      not locked; expires after 90d;
                                                      the owning domain may read its own datasets
```

**Bucket per source was rejected as the default.** It multiplies the configuration that must stay
identical (2 × N buckets of lifecycle, lock, encryption, replication), and every new partner would
need an infrastructure change. A source moves to its **own** bucket only on a hard trigger: data
residency or region, a contractual demand for physical separation, a partner-owned (cross-account)
bucket, or regulated data needing its own key and audit trail. The key layout is identical, so
that move is configuration, not code.

**Why two buckets and not one.** The inbox and the archive have opposite needs: partners write and
overwrite the inbox, the archive is immutable. Object lock can only be enabled when a bucket is
created and covers the whole bucket, so evidence and partner drops cannot share one.

### 2. No `processing/` prefix
S3 has no atomic move: a processing folder would cost a copy and a delete per file and add no
safety. The loader instead **pins the object version** it read from the inbox; a partner overwrite
mid-run is a new version, processed separately. "In flight" is a ledger status, not a folder.
Every processed object leaves the inbox (its version is deleted) after the ledger records the
outcome, so a bad file is never re-validated.

### 3. The partition is ours, derived from the contract
The partner's path is free-form under `<source>/<dataset>/`. The platform derives the partition
when it archives the file, from the landing contract's `customProperties`:

| Property | Values | Meaning |
|---|---|---|
| `partitionScheme` | `dt=YYYY-MM-DD` · `dt=YYYY-MM-DD/hr=HH` · `dt=YYYY-MM` | daily · hourly · monthly |
| `partitionSource` | `filename` · `arrival` | read the business date from the file name, or use arrival time |
| `filenamePattern` | regex with named groups `date` (YYYYMMDD / YYYY-MM-DD) or `year`/`month`/`day`/`hour` | the partner's own naming convention, described rather than imposed |
| `retentionDays` | `90` · `365` · `2555` | retention class of accepted files (one lifecycle rule each) |
| `completionSignal` | `per_file` · `settle` | when a delivery is ready (see §4) |
| `settleMinutes` | integer | quiet period for `settle` |

A file whose name does not match a declared `filenamePattern` is **quarantined** (check
`naming.filename_pattern`), partitioned by arrival time so it can still be triaged. The
`governance/policies/check_contracts.py` policy validates these properties in CI.

### 4. Batch completeness without changing the partner
The contract declares how the platform knows a delivery is complete. Implemented now:

- `per_file` (default): each file is a self-contained delivery and is processed when it arrives.
- `settle`: an object is processed only when it is older than `settleMinutes`, so a partner still
  uploading a batch is not processed half-way.

Designed, built with ADR-0016 (they need delivery grouping, which the event trigger brings):
`expected_files` (all of N named files present) and `marker` (only where the partner **already**
sends a `.ok`/`.done`/manifest). Every mode is bounded by the contract's `timeOfAvailability`
deadline: the platform never waits forever, it processes what arrived and alerts.

### 5. Access is generated from config
`platform/access/generate_landing_access.py` reads every `sources.yml` and generates, into
`platform/access/landing/`:

- `landing-partner-<source>`: `PutObject` (+ multipart) on `inbox/<source>/*` only. No list,
  read or delete; no other source's prefix; no archive.
- `landing-loader`: read/delete on the inbox; write (with retention and tags) and read on the
  archive. **No delete on the archive.**
- `landing-owner-<domain>`: read on `quarantine/<source>/<dataset>/*` for the datasets that domain
  owns (triage with the partner). Attached to the domain's SSO role in production.

The generated files are committed and CI fails on drift (`generate_landing_access.py --check`); `minio-init`
applies them at `make up`. The ingestion loader runs as `landing-loader`, not the MinIO root. In
the cloud the same documents become IAM / S3 Access Point policies.

### 6. Immutability: per-object lock, not a bucket default
The archive bucket has object lock **enabled with no default retention**. The loader sets the lock
on accepted objects only (`retain-until = archived + retentionDays`, mode `GOVERNANCE` locally,
`COMPLIANCE` in production). A bucket default would also lock quarantined files and they could not
expire at 90 days.

### 7. Gate results are published to DataHub
Each rule the gate executes is recorded per file in the ledger (`checks` JSON, with partition,
object version and archive key). The `publish_quality` task that follows `contract_gate` in every
ingestion DAG (and `make catalog-quality` for a full reconcile) runs
`platform/context/catalog/publish_landing_quality.py`, which projects them into DataHub:

- one **S3 dataset per landing dataset** (`<archive-bucket>/accepted/<source>/<dataset>`) with the
  contract schema, domain, owner, contract properties, and lineage into `<domain>_raw.<table>`;
- one **assertion per contract rule**, and one **assertion run event per file**, with the
  partition, the result and the quarantine/archive key.

The ledger stays the system of record; DataHub is the read model (ADR-0018).

## Consequences
- A rejected file leaves the inbox, and the evidence (file + `_reason.json`) sits in one
  predictable, short-lived, owner-readable place.
- Lifecycle: 2 inbox rules + 4 archive rules (3 retention classes + quarantine), constant as
  sources grow. Retention per dataset is a contract value checked in CI.
- Idempotency (P6) becomes "the latest accepted file for this dataset has the same content →
  skip" (decided by the gate, ADR-0025). A re-delivered identical file is recorded `skipped` and drained from the inbox.
- Replay/rebuild reads `accepted/` (`dgp-ingest replay --partition-date`), never the inbox; the
  restore drill (P9) uses it.
- Proof **P14** exercises the whole path on the live platform: partner permissions, quarantine
  with reason, filename-convention rejection, acceptance with lock and retention tag, redelivery
  skip, and the assertion results in DataHub.
- Not built yet (production): TLS-only and SSE-required bucket-policy denies (local MinIO runs
  plain HTTP), storage-class transitions (no tiers locally), per-partner KMS keys, and the event
  trigger itself (ADR-0016).
