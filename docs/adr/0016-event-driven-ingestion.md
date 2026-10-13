# ADR-0016: Event-driven ingestion for partner-pushed files

**Status:** Proposed (design; the PoC keeps the pull simulator until this lands)
**Extends** [ADR-0011](0011-landing-zone-slas-and-partitioning.md) (landing zone),
[ADR-0005](0005-airflow-orchestration.md) (orchestration).

## Context
Today ingestion is a **pull**: `platform_acquire_olist` downloads the Olist dataset on a schedule
and publishes `OLIST_CACHE`, and each domain's `land` step copies files from that cache into the
landing bucket. That is a faithful **PoC simulator of arrival** — but in production we do not
fetch partner data. Partners **push** files directly into our landing bucket on their own cadence.

The good news: the architecture already anticipates this. The **landing zone** and the
**contracts-at-landing** gate (ADR-0011, M2) exist precisely to validate data we did not produce.
What is PoC-only is the *trigger* and the *acquire/land* simulators — not the boundary.

## Decision

### 1. The trigger flips from scheduled pull to **event-driven**
`platform_acquire_olist` and the domain `land` step become **PoC-only**. For partner-sourced
datasets, arrival is an event, not a job we run:

```
partner → PUT object → landing bucket
        → bucket notification (MinIO / S3)
        → queue (SQS / SNS / EventBridge)
        → Airflow asset update (triggerer / sensor)
        → load_raw → contracts-at-landing gate → raw/<domain> asset
```

The `load_raw → validate → raw asset` path and the per-domain isolation (proof P1) are
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

### 4. Replay and audit come from the versioned landing bucket
The landing bucket is already **versioned with retention** (ADR-0011). That gives replay (reprocess
what a partner sent) and an audit trail of exactly what arrived and when — the ingestion-side
complement to the transform-side backfill in [ADR-0015](0015-scaling-the-transform-layer.md).

## Consequences
- No architectural break: the landing-zone + contract gate design was already the right shape for
  push; this ADR swaps the trigger and names the robustness requirements.
- New moving parts in production: bucket notifications, a queue, and an Airflow event source
  (triggerer/sensor), plus a dedup ledger and per-partner credential scoping.
- The PoC keeps the pull simulator (`platform_acquire_olist` + `land`) so the end-to-end demo runs
  offline; these are clearly marked PoC-only and are the first things replaced in a real deployment.
- Completeness and idempotency become explicit contract/operational obligations on producers, to be
  surfaced in the catalog and partner onboarding (DataHub, M4+).
