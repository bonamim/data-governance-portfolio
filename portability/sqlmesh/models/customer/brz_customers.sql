MODEL (
  name sqlmesh_customer_bronze.brz_customers,
  kind VIEW,
  description 'Bronze: standardised customers from raw (internal).'
);

SELECT
  customer_id,
  customer_unique_id,
  customer_zip_code_prefix,
  LOWER(customer_city) AS customer_city,
  UPPER(customer_state) AS customer_state
FROM customer_raw.customers
