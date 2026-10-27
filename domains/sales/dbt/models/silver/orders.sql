{{ config(
    access='public',
    materialized='incremental',
    incremental_strategy='microbatch',
    unique_key='order_id',
    event_time='order_purchase_timestamp',
    begin='2016-09-01',
    batch_size='month',
    lookback=1,
) }}
-- OUTPUT PORT: sales.orders, tagged `output_port` in its ODCS contract (the tag is
-- synced to dbt from the contract). A deliberate publish from the Silver layer.
-- Conformed orders in exactly the shape its ODCS contract promises consumers.
-- The order book grows by purchase month, so it is built in monthly microbatches (ADR-0015 §4):
-- a run processes only the latest batches, a backfill reprocesses a chosen window, and with
-- write-audit-publish each batch is audited before it is merged into the published table.
select
    order_id,
    customer_id,
    order_status,
    order_purchase_timestamp,
    order_approved_at,
    order_delivered_customer_date,
    order_estimated_delivery_date
from {{ ref('brz_orders') }}
