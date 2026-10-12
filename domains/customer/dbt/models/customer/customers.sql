-- Output port: customer.customers — the published Customer data product.
--
-- PII handling: the raw person identifier (customer_unique_id) is NEVER published.
-- It is replaced by a stable pseudonym so consumers can still count distinct people
-- and join across orders, without seeing the real value. md5 here is a demo;
-- production uses a keyed HMAC / pgcrypto with the key in a secrets manager
-- (production-readiness.md, PII row). Classified in the ODCS contract.
-- Public so other domains can ref it cross-project (dbt-loom); output ports only.
{{ config(access='public') }}

select
    customer_id,
    md5(customer_unique_id)   as customer_pseudonym,
    customer_zip_code_prefix,
    customer_city,
    customer_state
from {{ ref('stg_customers') }}
