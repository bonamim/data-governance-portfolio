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

Raw tables are created with **text columns** from the CSV header. Typing and cleaning are the
dbt *staging* layer's job (M3), which keeps this loader free of per-dataset schema knowledge.

## Design notes

- **State / idempotency** (ADR-0006, ADR-0024): every delivery is recorded in the ingestion ledger
  with its content SHA-256, pinned object version, partition, where it was filed and the per-rule
  gate results (`checks`, published to DataHub by `make catalog-quality`). A delivery whose content
  equals the dataset's latest accepted file is skipped by the gate (ADR-0025). Either way the inbox
  version is deleted, so a file is processed once. OLTP raw loads are full-refresh (truncate +
  reload).
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
