-- Bronze: standardized order items from raw (typed). Internal.
-- order_item_id is the line number within the order (1, 2, …), not an identifier on its own.
select
    order_id,
    order_item_id::int                    as order_item_id,
    product_id,
    seller_id,
    shipping_limit_date::timestamp        as shipping_limit_date,
    price::numeric                        as price,
    freight_value::numeric                as freight_value
from {{ source('sales_raw', 'order_items') }}
