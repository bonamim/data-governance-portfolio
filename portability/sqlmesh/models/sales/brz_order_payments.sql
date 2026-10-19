MODEL (
  name sqlmesh_sales_bronze.brz_order_payments,
  kind VIEW,
  description 'Bronze: typed order payments from raw (internal).'
);

SELECT
  order_id,
  payment_sequential::INT AS payment_sequential,
  payment_type,
  payment_installments::INT AS payment_installments,
  payment_value::NUMERIC AS payment_value
FROM sales_raw.order_payments
