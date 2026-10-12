-- Output port: sales.orders — the published Sales orders data product.
-- Consumers read this, never staging or raw. Its ODCS contract governs the shape.
select
    order_id,
    customer_id,
    order_status,
    order_purchase_timestamp,
    order_approved_at,
    order_delivered_customer_date,
    order_estimated_delivery_date
from {{ ref('stg_orders') }}
