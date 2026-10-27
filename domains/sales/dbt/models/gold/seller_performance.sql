{{ config(access='public') }}
-- OUTPUT PORT: sales.seller_performance, tagged `output_port` in its ODCS contract.
-- Sales performance per seller, for the marketplace domain's seller reviews.
-- total_revenue here is the value of the seller's items at their price, EXCLUDING freight,
-- not what customers paid (sales_performance.total_revenue, payments incl. freight and
-- instalment interest). Same name, two meanings: each column links its own ontology value
-- type (ItemsValue vs PaymentAmount, ADR-0027).
with items as (
    select order_id, seller_id, price, freight_value from {{ ref('brz_order_items') }}
)

select
    seller_id,
    -- COUNT is bigint in Postgres, but the contract promises integer.
    count(distinct order_id)::int  as orders_sold,
    count(*)::int                  as items_sold,
    round(sum(price), 2)           as total_revenue,
    round(sum(freight_value), 2)   as total_freight
from items
group by seller_id
