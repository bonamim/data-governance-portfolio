-- Typed/cleaned customers (engineering layer, internal). Still holds the raw PII
-- (customer_unique_id); pseudonymisation happens at the output port, not here.
with source as (
    select * from {{ source('customer_raw', 'customers') }}
),

typed as (
    select
        customer_id,
        customer_unique_id,
        customer_zip_code_prefix,
        lower(customer_city) as customer_city,
        upper(customer_state) as customer_state
    from source
)

select * from typed
