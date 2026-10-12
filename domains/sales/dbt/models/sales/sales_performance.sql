-- Output port: sales.sales_performance — a CONSUMER-ALIGNED data product.
-- Serves one decision context (performance reporting), so it is consumer-aligned,
-- not an aggregate (which would be general-purpose facts reused by 3+ teams).
-- Order and revenue performance by customer location, with average review score.
-- Consumes only published output ports:
--   sales.orders (own domain), customer.customers and customer_experience.reviews
--   (other domains, via dbt-loom, public models only). Never raw/staging of others.
{{ config(access='public') }}

with orders as (
    select * from {{ ref('orders') }}
),

revenue as (
    select order_id, sum(payment_value) as order_revenue
    from {{ ref('stg_order_payments') }}
    group by order_id
),

customers as (
    select customer_id, customer_state from {{ ref('dgp_customer', 'customers') }}
),

review as (
    select order_id, avg(review_score::numeric) as avg_review_score
    from {{ ref('dgp_customer_experience', 'reviews') }}
    group by order_id
)

select
    c.customer_state,
    count(distinct o.order_id)               as num_orders,
    count(distinct o.customer_id)            as num_customers,
    round(sum(r.order_revenue), 2)           as total_revenue,
    round(avg(rv.avg_review_score), 3)       as avg_review_score
from orders o
join customers c on o.customer_id = c.customer_id
left join revenue r on o.order_id = r.order_id
left join review rv on o.order_id = rv.order_id
group by c.customer_state
