# ADR-0016: Event-driven ingestion for partner-pushed files

**Status:** Proposed (design; the PoC keeps the pull simulator until this lands)
**Extends** [ADR-0011](0011-landing-zone-slas-and-partitioning.md) (landing zone),
[ADR-0005](0005-airflow-orchestration.md) (orchestration).

> **Update (ADR-0024):** the landing layout this ADR plugs into is now built: partners write to
> `landing-inbox/<source>/<dataset>/` with write-only credentials, the loader drains it into
> `landing-archive/accepted|quarantine/`, re-delivery is skipped by checksum, and the completeness
> signal is a contract property (`per_file`, `settle`; `expected_files`/`marker` come with this ADR).
> What remains here is the trigger: bucket notification → queue → Airflow.

## Context
Today ingestion is a **pull**: `platform_acquire_olist` downloads the Olist dataset on a schedule
and publishes `OLIST_CACHE`, and each domain's `land` step copies files from that cache into the
landing inbox (`landing-inbox`). That is a faithful **PoC simulator of arrival** — but in production we
do not fetch partner data. Partners **push** files directly into the inbox on their own cadence.

The good news: the architecture already anticipates this. The **landing zone** and the
**contracts-at-landing** gate (ADR-0011, M2) exist precisely to validate data we did not produce.
What is PoC-only is the *trigger* and the *acquire/land* simulators — not the boundary.

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
**unchanged** — only what *fires* it changes. Pull remains available for the datasets we genuinely
fetch (e.g. reference data).

### 2. The contracts-at-landing gate is the control point
Because we no longer control the producer, every arriving file is validated against its ODCS
contract **on arrival** (schema-on-arrival): conforming files proceed; non-conforming files are
**quarantined**, not loaded, and alerted. This gate already exists; push makes it load-bearing.

### 3. Robustness concerns push forces us to design for
| Concern | Decision |
|---|---|
| **Idempotency / dedup** | A partner may re-send a file. Track processed objects by **checksum / S3 object-version** in a ledger; re-delivery is a no-op. Never double-load. |
| **Batch completeness** | A delivery may be many files. Require a **manifest object or `_SUCCESS` marker** to mark a batch complete; handle partial / late-arriving files explicitly rather than processing half a batch. |
| **Per-partner security** | Partners get **scoped, write-only** credentials to **their own prefix** only (bucket policy / IAM). They never receive platform credentials and cannot read or overwrite others' data. |
| **Event fan-in at scale** | Do **not** launch a DAG per file. **Window / batch** events (micro-batch interval or queue drain) so a burst of thousands of objects is a bounded number of runs. |
| **Poison files** | A malformed or oversized drop is routed to **quarantine** and alerted; it must never block the pipeline for other files or partners. |

### 4. Replay and audit come from the landing archive
Accepted files sit in the **object-locked** `landing-archive/accepted/` with retention per contract
(ADR-0024), and `dgp-ingest replay` rebuilds raw from them. That gives replay (reprocess
what a partner sent) and an audit trail of exactly what arrived and when — the ingestion-side
complement to the transform-side backfill in [ADR-0015](0015-scaling-the-transform-layer.md).

### 5. Build checklist (carried from ADR-0024)
What ADR-0024 left open and this ADR's build must close:

| # | Item | Today | M10 |
|---|---|---|---|
| 1 | Arrival trigger | Airflow schedule on the PoC `olist-cache` asset | `landing-inbox` notification → queue → Airflow asset, windowed fan-in (no DAG per file) |
| 2 | Arrival in the catalog | Gate results published after each run (`publish_quality` task) | Also emit a DataHub *operation* at arrival, so freshness/timeliness are measured from landing time |
| 3 | Completion signals | `per_file`, `settle` | `expected_files`, `marker` (only where the partner already sends one); all bounded by `timeOfAvailability` |
| 4 | Alerting | Visible in the ledger, `_reason.json`, DataHub | Quarantine / incomplete delivery / SLA miss → owning domain's alert route |
| 5 | Dedup checksum | **Done (ADR-0025):** the gate computes the SHA-256 of the content it downloads and records it (`content_sha256`), so single-part and multipart uploads of the same bytes dedup | Optionally also verify a partner-sent `x-amz-checksum-sha256` on arrival |
| 6 | Bucket controls | SSE default, object lock, generated policies | Deny non-TLS + unencrypted uploads; storage-class transitions; per-partner KMS keys |
| 7 | Partner credentials | One `LANDING_PARTNER_<SOURCE>_SECRET` env var per source in `minio-init` | Provisioned per partner from the secrets manager (or SFTP/Transfer Family users mapped to their prefix) |

## Consequences
- No architectural break: the landing-zone + contract gate design was already the right shape for
  push; this ADR swaps the trigger and names the robustness requirements.
- New moving parts in production: bucket notifications, a queue, and an Airflow event source
  (triggerer/sensor), plus a dedup ledger and per-partner credential scoping.
- The PoC keeps the pull simulator (`platform_acquire_olist` + `land`) so the end-to-end demo runs
  offline; these are clearly marked PoC-only and are the first things replaced in a real deployment.
- Completeness and idempotency become explicit contract/operational obligations on producers, to be
  surfaced in the catalog and partner onboarding (DataHub, M4+).
