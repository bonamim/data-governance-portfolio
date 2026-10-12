{{ config(access='public') }}
-- OUTPUT PORT: sales.orders — tagged `output_port` in its ODCS contract (the tag is
-- synced to dbt from the contract). A deliberate publish from the Silver layer.
-- Conformed orders in exactly the shape its ODCS contract promises consumers.
select
    order_id,
    customer_id,
    order_status,
    order_purchase_timestamp,
    order_approved_at,
    order_delivered_customer_date,
    order_estimated_delivery_date
from {{ ref('brz_orders') }}
