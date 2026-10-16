-- Bronze: standardized orders from raw (typed, empty strings -> NULL). Internal.
-- event_time lets the microbatch port (sales.orders) read only each batch's rows.
{{ config(event_time='order_purchase_timestamp') }}
select
    order_id,
    customer_id,
    order_status,
    nullif(order_purchase_timestamp, '')::timestamp      as order_purchase_timestamp,
    nullif(order_approved_at, '')::timestamp             as order_approved_at,
    nullif(order_delivered_carrier_date, '')::timestamp  as order_delivered_carrier_date,
    nullif(order_delivered_customer_date, '')::timestamp as order_delivered_customer_date,
    nullif(order_estimated_delivery_date, '')::timestamp as order_estimated_delivery_date
from {{ source('sales_raw', 'orders') }}
