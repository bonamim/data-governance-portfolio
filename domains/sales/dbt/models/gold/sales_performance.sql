{{ config(access='public') }}
-- OUTPUT PORT: sales.sales_performance — tagged `output_port` in its ODCS contract.
-- A deliberate publish from Gold (the tag is synced to dbt from the contract).
-- Order/revenue performance by customer state with average review score.
-- Consumes other domains' output ports only, via dbt-loom (public models):
-- customer.customers and customer_experience.reviews.
with orders as (
    select * from {{ ref('orders') }}
),

revenue as (
    select order_id, sum(payment_value) as order_revenue
    from {{ ref('brz_order_payments') }}
    group by order_id
),

customers as (
    -- customer_unique_id arrives pseudonymised (ADR-0013); counting distinct hashes counts people.
    select customer_id, customer_unique_id, customer_state from {{ ref('dgp_customer', 'customers') }}
),

review as (
    select order_id, avg(review_score::numeric) as avg_review_score
    from {{ ref('dgp_customer_experience', 'reviews') }}
    group by order_id
)

select
    c.customer_state,
    -- COUNT is bigint in Postgres; the contract promises integer.
    count(distinct o.order_id)::int    as num_orders,
    count(distinct o.customer_id)::int as num_customers,
    count(distinct c.customer_unique_id)::int as num_people,
    round(sum(r.order_revenue), 2)     as total_revenue,
    round(avg(rv.avg_review_score), 3) as avg_review_score
from orders o
join customers c on o.customer_id = c.customer_id
left join revenue r on o.order_id = r.order_id
left join review rv on o.order_id = rv.order_id
group by c.customer_state
