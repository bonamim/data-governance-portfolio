{{ config(access='public') }}
-- OUTPUT PORT: customer.customers — tagged `output_port` in its ODCS contract.
-- A deliberate publish from Silver (the tag is synced to dbt from the contract).
-- PII governance is a ROLE-BASED POLICY, not a static transform (ADR-0013): the raw person
-- identifier is stored here (protected at rest) and masked AT READ by role — consumers read the
-- masking view (customer_masked.customers) and see a pseudonym; an unmask role (compliance) sees
-- the raw value. The base table is never granted to consumers (platform/access, proof P8).
select
    customer_id,
    customer_unique_id,
    customer_zip_code_prefix,
    customer_city,
    customer_state
from {{ ref('brz_customers') }}
