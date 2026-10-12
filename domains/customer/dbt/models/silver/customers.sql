{{ config(access='public') }}
-- OUTPUT PORT: customer.customers — tagged `output_port` in its ODCS contract.
-- A deliberate publish from Silver (the tag is synced to dbt from the contract).
-- PII is masked at the port: the raw person identifier (held internally in Bronze)
-- is never published; consumers get a stable pseudonym. md5 here is a demo;
-- production uses a keyed HMAC via a secrets manager (production-readiness.md).
select
    customer_id,
    md5(customer_unique_id)   as customer_pseudonym,
    customer_zip_code_prefix,
    customer_city,
    customer_state
from {{ ref('brz_customers') }}
