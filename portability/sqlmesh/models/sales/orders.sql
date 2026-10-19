MODEL (
  name sqlmesh_sales_silver.orders,
  -- dbt: incremental microbatch by month (ADR-0015). SQLMesh: the engine itself records which
  -- monthly intervals are built (its state), and `plan --restate-model` reprocesses a range.
  kind INCREMENTAL_BY_TIME_RANGE (
    time_column order_purchase_timestamp
  ),
  columns (
    order_id TEXT,
    customer_id TEXT,
    order_status TEXT,
    order_purchase_timestamp TIMESTAMP,
    order_approved_at TIMESTAMP,
    order_delivered_customer_date TIMESTAMP,
    order_estimated_delivery_date TIMESTAMP
  ),
  interval_unit 'month',
  -- SQLMesh builds only COMPLETE intervals by default (dbt microbatch also builds the current
  -- partial month): the cron matches the interval.
  cron '@monthly',
  grain order_id,
  description 'OUTPUT PORT sales.orders — same contract as the dbt build.',
  audits (
    -- generated from domains/sales/contracts/orders.odcs.yaml (sync_contracts.py)
    not_null(columns := (order_id, customer_id, order_status, order_purchase_timestamp)),
    unique_values(columns := (order_id))
  )
);

SELECT
  order_id,
  customer_id,
  order_status,
  order_purchase_timestamp,
  order_approved_at,
  order_delivered_customer_date,
  order_estimated_delivery_date
FROM sqlmesh_sales_bronze.brz_orders
WHERE order_purchase_timestamp BETWEEN @start_ts AND @end_ts
