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
- `type: landing` — uploaded to the MinIO landing zone, then loaded back to `<domain>_raw`
  (idempotent by ETag via the ingestion ledger).

## What the platform does

| Step | Command | Effect |
|---|---|---|
| acquire | `dgp-ingest acquire` | Download the Olist dataset from Kaggle into `data/olist/` |
| seed | `dgp-ingest seed` | Load operational CSVs into `shop_oltp.shop.*` |
| land | `dgp-ingest land` | Upload file-sourced CSVs to `s3://landing/<domain>/<name>/` |
| load-raw | `dgp-ingest load-raw` | Load every dataset into `<domain>_raw`, writing `platform_meta.ingestion_ledger` |
| all | `dgp-ingest all` | acquire → seed → land → load-raw |

Raw tables are created with **text columns** from the CSV header; typing and cleaning are the
dbt *staging* layer's job (M3), which keeps this loader free of per-dataset schema knowledge.

## Design notes

- **State / idempotency** (ADR-0006): file loads are recorded in the ingestion ledger with the
  object ETag; an unchanged file is skipped. OLTP raw loads are full-refresh (truncate + reload).
- **Least privilege**: seeding uses `oltp_loader`; reading the source uses `oltp_reader`;
  writing the warehouse uses `platform_loader`. No superuser at runtime.
- **The Airflow DAGs call `pipeline.py` directly** — the CLI is for local runs and the Makefile.
