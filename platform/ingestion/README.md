# platform/ingestion (`dgp-platform-ingestion`)

Self-serve ingestion capability. This is **platform code**: it is generic and driven
entirely by the domains' declarative config, so a domain onboards a source without
touching this package (ADR-0008).

## What a domain provides

A `sources.yml` in `domains/<domain>/ingestion/` (or `reference/ingestion/`):

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

- `type: oltp` — seeded into `shop_oltp`, then loaded to `<domain>_raw`.
- `type: landing` — delivered by the partner into the landing **inbox**, gated against the ODCS
  landing contract, filed to the **archive** (`accepted/` or `quarantine/`), then loaded to
  `<domain>_raw` (ADR-0024). `delivered_as` is only for the PoC partner simulator (its file name).

## What the platform does

| Step | Command | Effect |
|---|---|---|
| acquire | `dgp-ingest acquire` | Download the Olist dataset from Kaggle into `data/olist/` |
| seed | `dgp-ingest seed` | Load operational CSVs into `shop_oltp.shop.*` |
| land | `dgp-ingest land` | PoC partner simulator: PUT files into `s3://landing-inbox/<source>/<dataset>/` as the write-only partner |
| load-raw | `dgp-ingest load-raw` | Load OLTP datasets; drain each landing inbox through the contract gate into `accepted/` (loaded) or `quarantine/` (+ `_reason.json`), writing `platform_meta.ingestion_ledger` |
| replay | `dgp-ingest replay --partition-date D` | Rebuild raw from `shop_oltp` + the archive's `accepted/` files for `dt=D` (restore drill, backfill); never touches the inbox |
| all | `dgp-ingest all` | acquire → seed → land → load-raw |

Raw tables are created with **text columns** from the CSV header; typing and cleaning are the
dbt *staging* layer's job (M3), which keeps this loader free of per-dataset schema knowledge.

## Design notes

- **State / idempotency** (ADR-0006, ADR-0024): every delivery is recorded in the ingestion ledger
  with its checksum, pinned object version, partition, where it was filed and the per-rule gate
  results (`checks`, published to DataHub by `make catalog-quality`). A delivery whose checksum
  equals the dataset's latest load is skipped; either way the inbox version is deleted, so a file is
  processed once. OLTP raw loads are full-refresh (truncate + reload).
- **Landing layout** (ADR-0024):

  ```
  landing-inbox/<source>/<dataset>/<partner's file>                       partner: write-only
  landing-archive/accepted/<source>/<dataset>/<partition>/<file>           locked, retention-tagged
  landing-archive/quarantine/<source>/<dataset>/<partition>/<run_id>/      file + _reason.json, 90d
  ```

  The partition is derived from the contract (`partitionScheme`, `partitionSource`,
  `filenamePattern`), never imposed on the partner. The loader runs as `landing-loader`
  (`platform/access/landing/`), not the object-store root.
- **Least privilege**: seeding uses `oltp_loader`; reading the source uses `oltp_reader`;
  writing the warehouse uses `platform_loader`. No superuser at runtime.
- **The Airflow DAGs call `pipeline.py` directly** — the CLI is for local runs and the Makefile.
