# ADR-0016: Event-driven ingestion for partner-pushed files

**Status:** Accepted, implemented in M10 (2026-10-16). See *Implementation* below
**Extends** [ADR-0011](0011-landing-zone-slas-and-partitioning.md) (landing zone),
[ADR-0005](0005-airflow-orchestration.md) (orchestration).

> **Update (ADR-0024):** the landing layout this ADR plugs into is now built: partners write to
> `landing-inbox/<source>/<dataset>/` with write-only credentials, the loader drains it into
> `landing-archive/accepted|quarantine/`, re-delivery is skipped by checksum, and the completeness
> signal is a contract property (`per_file` and `settle`, with `expected_files`/`marker` added by this ADR).
> What remains here is the trigger: bucket notification → queue → Airflow.

## Context
Today ingestion is a **pull**: `platform_acquire_olist` downloads the Olist dataset on a schedule
and publishes `OLIST_CACHE`, and each domain's `land` step copies files from that cache into the
landing inbox (`landing-inbox`). That is a faithful **PoC simulator of arrival**, but in production we
do not fetch partner data. Partners **push** files directly into the inbox on their own cadence.

The good news: the architecture already anticipates this. The **landing zone** and the
**contracts-at-landing** gate (ADR-0011, M2) exist precisely to validate data we did not produce.
What is PoC-only is the *trigger* and the *acquire/land* simulators, not the boundary.

## Decision

### 1. The trigger flips from scheduled pull to **event-driven**
`platform_acquire_olist` and the domain `land` step become **PoC-only**. For partner-sourced
datasets, arrival is an event, not a job we run:

```
partner → PUT object → landing-inbox/<source>/<dataset>/
        → bucket notification (MinIO / S3)
        → queue (SQS / SNS / EventBridge)
        → Airflow asset update (triggerer / sensor)
        → contract_gate → load_raw → raw/<domain> asset
```

The `contract_gate → load_raw → raw asset` path (ADR-0025) and the per-domain isolation (proof P1) are
**unchanged**. Only what *fires* it changes. Pull remains available for the datasets we genuinely
fetch (e.g. reference data).

### 2. The contracts-at-landing gate is the control point
Because we no longer control the producer, every arriving file is validated against its ODCS
contract **on arrival** (schema-on-arrival): conforming files proceed, and non-conforming files are
**quarantined**, not loaded, and alerted. This gate already exists, and push makes it load-bearing.

### 3. Robustness concerns push forces us to design for
| Concern | Decision |
|---|---|
| **Idempotency / dedup** | A partner may re-send a file. Track processed objects by **checksum / S3 object-version** in a ledger, so re-delivery is a no-op. Never double-load. |
| **Batch completeness** | A delivery may be many files. Require a **manifest object or `_SUCCESS` marker** to mark a batch complete, and handle partial / late-arriving files explicitly rather than processing half a batch. |
| **Per-partner security** | Partners get **scoped, write-only** credentials to **their own prefix** only (bucket policy / IAM). They never receive platform credentials and cannot read or overwrite others' data. |
| **Event fan-in at scale** | Do **not** launch a DAG per file. **Window / batch** events (micro-batch interval or queue drain) so a burst of thousands of objects is a bounded number of runs. |
| **Poison files** | A malformed or oversized drop is routed to **quarantine** and alerted. It must never block the pipeline for other files or partners. |

### 4. Replay and audit come from the landing archive
Accepted files sit in the **object-locked** `landing-archive/accepted/` with retention per contract
(ADR-0024), and `dgp-ingest replay` rebuilds raw from them. That gives replay (reprocess
what a partner sent) and an audit trail of exactly what arrived and when. It is the ingestion-side
complement to the transform-side backfill in [ADR-0015](0015-scaling-the-transform-layer.md).

### 5. Build checklist (carried from ADR-0024)
What ADR-0024 left open and this ADR's build must close:

| # | Item | Today | M10 |
|---|---|---|---|
| # | Item | Status (M10) |
|---|---|---|
| 1 | Arrival trigger | **Built.** `landing-inbox` notification → queue → Airflow `AssetWatcher` → `landing/<domain>`, with windowed fan-in. Proof P17 |
| 2 | Arrival in the catalog | **Built.** One DataHub `ARRIVAL` operation per file, stamped with its landing time. P17 |
| 3 | Completion signals | **Built.** `expected_files`, `marker`, decided per partition batch, with deadlines from `timeOfAvailability`. P18 |
| 4 | Alerting | **Built.** Quarantine and SLA misses (`missing` / `incomplete` / `rejected`) → the domain's route, once. P17, P18 |
| 5 | Dedup checksum | **Built.** Content SHA-256 (ADR-0025), plus a partner-sent `x-amz-checksum-sha256` is verified. P14, P17 |
| 6 | Bucket controls | **Specified as code.** TLS-only, SSE-KMS per partner key, storage-class transitions: generated, drift-checked and tested, applied in production (D11) |
| 7 | Partner credentials | **Specified as code.** One secrets-manager entry per principal, 90-day rotation (D12). Locally still env vars |

## Implementation (M10, 2026-10-16)

**The queue.** MinIO's PostgreSQL notification target appends every `s3:ObjectCreated` in the inbox
to `landing_events.arrivals` (S3 event JSON, the same records SQS delivers), with a persistent
queue directory so events survive a database outage. The `landing_events` role owns only that
schema. The semantics are SQS's: Airflow's trigger *signals* rows (`signalled_at`), the contract
gate *acks* (deletes) a row once its file has left the inbox, and a row still present after the
visibility timeout (10 min) is signalled again. The gate drains the whole inbox whatever fired
it, so a duplicate or lost signal changes *when* a file is processed, never *whether*. Rows no
prefix ever acks are purged after 7 days by the SLA monitor.

**The trigger.** `LandingArrivalTrigger` (an Airflow 3 `BaseEventTrigger`, run by the triggerer
through an `AssetWatcher` on `landing/<domain>`) polls every 10 s. When something arrives it waits
30 s for the rest of the burst and fires once for everything due, so P17's five-file burst is one
run. In AWS the class is swapped for the common-messaging `MessageQueueTrigger` on SQS, and nothing
else changes.

**The DAGs.** The simulators left the domain DAGs: `platform_source_simulator` (PoC only) seeds the
operational DB (publishing `shop-oltp`) and delivers each partner's files as that partner. A
domain's `<domain>_ingestion` is scheduled on `landing/<domain>` and/or `shop-oltp`, with
`max_active_runs=1` (arrivals during a run queue one more run). `platform_landing_sla` runs every
15 minutes.

**Batches.** Under `expected_files` / `marker` the gate decides a whole partition at once: accepted
only if every file passes, otherwise every file is quarantined under one `_reason.json` that lists
the batch and names the failing file. `load_raw` loads the newest batch (first file replaces the
raw table, the rest append, one transaction). A marker is consumed when its batch is decided.
Dedup compares a batch's set of content hashes with the newest good batch.

**Deadlines.** By `timeOfAvailability` on day D, the partition `latency` days earlier is due. Past
the deadline it is `delivered`, `incomplete` (files waiting on their completion signal),
`rejected` (only quarantined files) or `missing`. Anything but `delivered` alerts.

**Alerts.** Routes live in the domain registry (`alerts: {channel, webhook_env}`), and the webhook URL
is a secret from the environment. Every alert is recorded in `platform_meta.alerts` under a dedup
key (one per batch for a quarantine, one per partition for a deadline), so a condition that
persists alerts once. An unreachable route is recorded (`delivered=false`) and never fails the
pipeline.

**Freshness.** `publish_quality` emits one `ARRIVAL` operation per file with
`lastUpdatedTimestamp` = when it landed. S3 times are to the second, so the aspect's own timestamp
is the ledger's recording time. Otherwise files landing in the same second overwrite each other.

**Guards found while proving it.** A partner file whose business date is in the future is
quarantined (`naming.business_date`): archived as accepted, it would pose as the newest data, and
the restore drill, which replays "as of the newest partition", picked such a file once. The drill
now also ignores partitions dated after today.

**Production controls.** `generate_landing_access.py` also generates `platform/access/landing/production/`:
bucket policies (deny `aws:SecureTransport=false`, deny uploads that are not SSE-KMS, each partner
prefix only under that partner's key, nobody deletes `accepted/`), lifecycles with storage-class
transitions per retention class (`STANDARD_IA` 30 d, `GLACIER_IR` 90 d, `DEEP_ARCHIVE` 365 d for
the 7-year class), one KMS key per partner, and the secrets-manager inventory. CI fails on drift and
unit tests pin the guarantees. They need TLS on the object store, a KMS, a remote storage tier and
a secrets manager, so they are applied by the production deployment (production-readiness D11,
D12), like M8's desired controls (ADR-0023).

## Consequences
- No architectural break: the landing-zone + contract gate design was already the right shape for
  push. This ADR swaps the trigger and names the robustness requirements.
- New moving parts in production: bucket notifications, a queue, and an Airflow event source
  (triggerer/sensor), plus a dedup ledger and per-partner credential scoping.
- The PoC keeps the pull simulator (`platform_acquire_olist` + `land`) so the end-to-end demo runs
  offline. These are clearly marked PoC-only and are the first things replaced in a real deployment.
- Completeness and idempotency become explicit contract/operational obligations on producers, to be
  surfaced in the catalog and partner onboarding (DataHub, M4+).
