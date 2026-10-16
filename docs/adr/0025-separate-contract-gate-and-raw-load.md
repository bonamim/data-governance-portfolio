# ADR-0025: Separate the contract gate from the raw load

**Status:** Accepted
**Amends** [ADR-0024](0024-landing-zone-layout-access-and-lifecycle.md) §7 and its idempotency
consequence (the gate, not the load, decides `skipped`).

## Context
The landing quality check already ran *before* the load, but both lived inside one Airflow task
called `load_raw`. For each inbox file that task pinned the version, ran the contract gate, filed
the file to `accepted/` or `quarantine/`, loaded it if accepted, wrote the ledger row and drained
the inbox. A `publish_quality` task then copied the results to DataHub.

Reading the DAG, it looked the other way round: `load_raw → publish_quality` suggests the data is
loaded and then checked. When a file was quarantined, `load_raw` was green and the red assertion
appeared in DataHub after it, so people read it as "bad data was loaded". The gate was invisible in
the orchestrator, and a failed load could not be retried without re-validating every file.

## Decision

### 1. Two tasks, in the order things happen
```
land → contract_gate → load_raw
                     → publish_quality
seed ────────────────→ load_raw          (OLTP datasets)
```

- **`contract_gate`** (`pipeline.contract_gate`, `dgp-ingest gate`) drains the inbox: every settled
  delivery is checked against its ODCS contract and filed to `accepted/` (locked, ledger
  `accepted`) or `quarantine/` (+ `_reason.json`, ledger `quarantined`), or recorded `skipped` if
  its content equals the dataset's latest accepted file (§3). **It writes nothing to the warehouse.**
- **`load_raw`** (`pipeline.load_raw`, `dgp-ingest load-raw`) loads OLTP datasets and, for each
  landing dataset, the accepted file that no load has picked up yet. **It never reads the inbox and
  never re-checks a file.**
- **`publish_quality`** hangs off the gate, not the load, so a failed load cannot hide a
  quarantine from DataHub.

### 2. The ledger is the hand-off
No new storage: the gate's `accepted` row is the work item. A load writes a `loaded` row whose new
`gate_id` column points at the `accepted` row it loaded. *Pending* = an `accepted` row that no row
points at. The load reads the exact archive version the gate filed (new `archive_version` column),
so a later re-delivery under the same name cannot change what is loaded.

Raw holds one file per dataset (each load replaces the table). When several files are pending, the
newest is loaded and the older ones are recorded `superseded` (with `gate_id`), which gives the
same end state as loading them one after another. Status values: `accepted`, `loaded`,
`quarantined`, `skipped`, `superseded`. The loader migrates existing ledgers itself.

### 3. Dedup on content, not the ETag
The ETag of the same bytes differs between a single-part and a multipart upload, so a partner
re-sending a file with another client loaded it again. The gate downloads every well-named file
anyway, so it computes the SHA-256 of the content and compares that with the dataset's latest
`accepted`/`loaded` row (new `content_sha256` column, carried onto the load row). Rows written
before the column existed fall back to the ETag, so the first re-delivery after upgrading behaves
as before. The cost is that a duplicate is downloaded before it is skipped.

### 4. Replay as of a date
`dgp-ingest replay --partition-date D` loads, for each landing dataset, its newest accepted file on
or before `D`. It used to require a file in `dt=D` for every dataset, which broke the restore drill
(P9) as soon as datasets delivered on different days.

### 5. Crash safety
- Gate: archive copy → ledger commit → delete the inbox version. A crash before the commit leaves
  the file in the inbox and the next run redoes it; a crash after it is caught by the content
  check and the file is drained as `skipped`.
- Load: the table load and its ledger rows are one transaction, so either raw holds the file and
  the ledger says so, or neither does and the retry loads it again.

## Consequences
- The Airflow graph shows the check before the load; a quarantine is a visible gate run with no
  load behind it.
- `load_raw` is retryable on its own (Airflow retries, or a clear of just that task) without
  re-checking files or touching the inbox.
- DataHub run events come from the gate rows (the ones with `checks`); their outcome is now
  `accepted` or `quarantined`. Load rows carry no checks and are not published.
- `dgp-ingest all` runs `acquire → seed → land → gate → load-raw`. By hand, a landing test is
  `dgp-ingest gate` then `dgp-ingest load-raw`.
- Proof P14 now proves the split: the gate accepts a file and the raw table stays untouched; the
  load then loads it and its row points at the gate row; a retried load loads nothing.
- Replay (`dgp-ingest replay`) still reads `accepted/` directly, never the inbox (§4).
