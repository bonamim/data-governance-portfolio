MODEL (
  name sqlmesh_sales_bronze.brz_orders,
  kind VIEW,
  description 'Bronze: typed orders from raw (internal).'
);

SELECT
  order_id,
  customer_id,
  order_status,
  NULLIF(order_purchase_timestamp, '')::TIMESTAMP AS order_purchase_timestamp,
  NULLIF(order_approved_at, '')::TIMESTAMP AS order_approved_at,
  NULLIF(order_delivered_carrier_date, '')::TIMESTAMP AS order_delivered_carrier_date,
  NULLIF(order_delivered_customer_date, '')::TIMESTAMP AS order_delivered_customer_date,
  NULLIF(order_estimated_delivery_date, '')::TIMESTAMP AS order_estimated_delivery_date
FROM sales_raw.orders
