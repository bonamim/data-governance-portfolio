MODEL (
  name sqlmesh_sales_gold.sales_performance,
  kind FULL,
  columns (
    customer_state TEXT,
    num_orders INTEGER,
    num_customers INTEGER,
    total_revenue NUMERIC,
    avg_review_score NUMERIC
  ),
  description 'OUTPUT PORT sales.sales_performance — same contract as the dbt build. Cross-domain inputs are plain model references here (one SQLMesh project), dbt-loom refs in dbt.',
  audits (
    -- generated from domains/sales/contracts/sales_performance.odcs.yaml (sync_contracts.py)
    not_null(columns := (customer_state, num_orders, num_customers, total_revenue)),
    unique_values(columns := (customer_state)),
    number_of_rows(threshold := 10)
  )
);

WITH orders AS (
  SELECT * FROM sqlmesh_sales_silver.orders
), revenue AS (
  SELECT order_id, SUM(payment_value) AS order_revenue
  FROM sqlmesh_sales_bronze.brz_order_payments
  GROUP BY order_id
), customers AS (
  SELECT customer_id, customer_state FROM sqlmesh_customer_silver.customers
), review AS (
  SELECT order_id, AVG(review_score::NUMERIC) AS avg_review_score
  FROM sqlmesh_customer_experience_silver.reviews
  GROUP BY order_id
)
SELECT
  c.customer_state,
  COUNT(DISTINCT o.order_id)::INT AS num_orders,
  COUNT(DISTINCT o.customer_id)::INT AS num_customers,
  ROUND(SUM(r.order_revenue), 2) AS total_revenue,
  ROUND(AVG(rv.avg_review_score), 3) AS avg_review_score
FROM orders AS o
JOIN customers AS c ON o.customer_id = c.customer_id
LEFT JOIN revenue AS r ON o.order_id = r.order_id
LEFT JOIN review AS rv ON o.order_id = rv.order_id
GROUP BY c.customer_state
