-- Bronze: standardized order payments from raw (typed). Internal.
select
    order_id,
    payment_sequential::int   as payment_sequential,
    payment_type,
    payment_installments::int as payment_installments,
    payment_value::numeric    as payment_value
from {{ source('sales_raw', 'order_payments') }}
