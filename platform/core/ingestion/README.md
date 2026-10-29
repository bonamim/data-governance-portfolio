# platform/core/ingestion (`dgp-platform-ingestion`)

Self-serve ingestion capability. This is **platform code**: it is generic and driven
entirely by the domains' declarative config, so a domain onboards a source without
touching this package (ADR-0008).

## What a domain provides

A `sources.yml` in `domains/<domain>/ingestion/`:

```yaml
domain: sales
datasets:
  - name: orders
    source: {type: oltp, olist_file: olist_orders_dataset.csv, oltp_table: orders}
    target: {raw_table: orders}
  - name: reviews
    source: {type: landing, olist_file: olist_order_reviews_dataset.csv}
    target: {raw_table: reviews}
```

- `type: oltp`: seeded into `shop_oltp`, then loaded to `<domain>_raw`.
- `type: landing`: delivered by the partner into the landing **inbox**, gated against the ODCS
  landing contract, filed to the **archive** (`accepted/` or `quarantine/`), then loaded to
  `<domain>_raw` (ADR-0024). `delivered_as` is only for the PoC partner simulator (its file name).

## What the platform does

| Step | Command | Effect |
|---|---|---|
| acquire | `dgp-ingest acquire` | Download the Olist dataset from Kaggle into `data/olist/` |
| seed | `dgp-ingest seed` | Load operational CSVs into `shop_oltp.shop.*` |
| land | `dgp-ingest land` | PoC partner simulator: PUT files into `s3://landing-inbox/<source>/<dataset>/` as the write-only partner |
| gate | `dgp-ingest gate` | Drain each landing inbox through the contract gate into `accepted/` (ledger `accepted`) or `quarantine/` (+ `_reason.json`). Loads nothing |
| load-raw | `dgp-ingest load-raw` | Load OLTP datasets, and for each landing dataset the `accepted/` file no load has picked up yet (ADR-0025), writing `platform_meta.ingestion_ledger` |
| sla | `dgp-ingest sla` | Check each landing dataset's due partition against its contract deadline (`timeOfAvailability`), and alert the owner on `missing` / `incomplete` / `rejected` (ADR-0016) |
| replay | `dgp-ingest replay --partition-date D` | Rebuild raw from `shop_oltp` + each dataset's newest `accepted/` file on or before `D` (restore drill, backfill). Never touches the inbox |
| all | `dgp-ingest all` | acquire → seed → land → gate → load-raw |
| governance-preview | `dgp-ingest governance-preview [--warehouse snowflake]` | Print the SQL the loader runs on each raw table when it creates it (below). Needs no connection |

Raw tables are created with **text columns** from the CSV header. Typing and cleaning are the
dbt *staging* layer's job (M3), which keeps this loader free of per-dataset schema knowledge.

**Every raw table is governed by the load that writes it** (`governance.py`, ADR-0027). As soon as
the table exists (created, or emptied for a reload) and before its first row is written, the loader
writes the dataset's resolved source contract (`contracts/sources/`, a landing file's or an
operational table's alike) onto it: the table's and each column's description as a `COMMENT`, and on
a warehouse with object tags (`WAREHOUSE_PLATFORM=snowflake`) each column's sensitivity and privacy
category and the table's sensitivity, the values the dbt build sets on a port. The platform's system
columns (`_system_batch_id`, `_system_source_path`, `_system_partition`, `_system_loaded_at`,
ADR-0028) follow the delivered ones on every raw table, OLTP included: the loader stamps each file's
values on its rows in the same COPY, and a table from before them gains them in place, never
dropped. So no row is ever in
an ungoverned table, even where DDL commits on its own (Snowflake). It runs on every load and replay,
since a recreated table loses both. On Postgres the catalog carries the tags.

## Design notes

- **State / idempotency** (ADR-0006, ADR-0024): every delivery is recorded in the ingestion ledger
  with its content SHA-256, pinned object version, partition, where it was filed and the per-rule
  gate results (`checks`, published to DataHub by `make catalog-quality`). A delivery whose content
  equals the dataset's latest accepted file is skipped by the gate (ADR-0025). Either way the inbox
  version is deleted, so a file is processed once. OLTP raw loads are full-refresh (truncate +
  reload), and an OLTP table whose columns no longer match its contract fails its load: a renamed
  or dropped source column stops at the boundary, as a landing file that breaks its contract is
  quarantined.
- **Headers**: a CSV header's UTF-8 byte-order mark is not part of its first column's name (the
  Olist translation file starts with one, and it used to name `shop_oltp` and raw columns).
- **Completion signals** (ADR-0016): `per_file` / `settle` decide each file alone.
  `expected_files` (`expectedFiles: N`) and `marker` (`completionMarker`, a regex with the
  partition's date groups) decide a whole partition as one batch (all accepted, or all quarantined
  under one `_reason.json`), and `load_raw` loads the batch together. A marker is consumed.
- **Arrivals** (ADR-0016): uploads land in `landing_events.arrivals` (bucket notifications), and the gate
  acks the rows of files that have left the inbox (`arrivals.py`). A partner-sent
  `x-amz-checksum-sha256` is verified (`integrity.checksum_sha256`).
- **Alerts** (`alerts.py`): quarantines and deadline misses go to the domain's registry route, once
  per condition (`platform_meta.alerts`).
- **Landing layout** (ADR-0024):

  ```
  landing-inbox/<source>/<dataset>/<partner's file>                       partner: write-only
  landing-archive/accepted/<source>/<dataset>/<partition>/<file>           locked, retention-tagged
  landing-archive/quarantine/<source>/<dataset>/<partition>/<run_id>/      file + _reason.json, 90d
  ```

  The partition is derived from the contract (`partitionScheme`, `partitionSource`,
  `filenamePattern`), never imposed on the partner. The loader runs as `landing-loader`
  (`platform/access/landing/`), not the object-store root.
- **Least privilege**: seeding uses `oltp_loader`, reading the source uses `oltp_reader`, and
  writing the warehouse uses `platform_loader`. No superuser at runtime.
- **The Airflow DAGs call `pipeline.py` directly**. The CLI is for local runs and the Makefile.
