-- Typed, cleaned orders from the raw landing/text layer (engineering layer).
-- Empty strings become NULL; timestamps are cast. Internal — not an output port.
with source as (
    select * from {{ source('sales_raw', 'orders') }}
),

typed as (
    select
        order_id,
        customer_id,
        order_status,
        nullif(order_purchase_timestamp, '')::timestamp      as order_purchase_timestamp,
        nullif(order_approved_at, '')::timestamp             as order_approved_at,
        nullif(order_delivered_carrier_date, '')::timestamp  as order_delivered_carrier_date,
        nullif(order_delivered_customer_date, '')::timestamp as order_delivered_customer_date,
        nullif(order_estimated_delivery_date, '')::timestamp as order_estimated_delivery_date
    from source
)

select * from typed
