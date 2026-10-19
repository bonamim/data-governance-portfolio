MODEL (
  name sqlmesh_customer_silver.customers,
  kind VIEW,
  description 'OUTPUT PORT customer.customers — same contract as the dbt build.',
  audits (
    -- generated from domains/customer/contracts/customers.odcs.yaml (sync_contracts.py)
    not_null(columns := (customer_id, customer_unique_id, customer_zip_code_prefix, customer_city, customer_state)),
    unique_values(columns := (customer_id)),
    number_of_rows(threshold := 1000)
  )
);

SELECT
  customer_id,
  customer_unique_id,
  customer_zip_code_prefix,
  customer_city,
  customer_state
FROM sqlmesh_customer_bronze.brz_customers
