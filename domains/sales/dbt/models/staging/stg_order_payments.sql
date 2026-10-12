-- Typed order payments (engineering layer, internal). Used to derive order revenue.
with source as (
    select * from {{ source('sales_raw', 'order_payments') }}
),

typed as (
    select
        order_id,
        payment_sequential::int   as payment_sequential,
        payment_type,
        payment_installments::int as payment_installments,
        payment_value::numeric    as payment_value
    from source
)

select * from typed
