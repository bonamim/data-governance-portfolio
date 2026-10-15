# ADR-0013: Role-based dynamic data masking (declared in ODCS, enforced per platform)

**Status:** Accepted (implemented locally in M8 — `platform/access/apply_masking.py`, proof P8)

## Context
PII must be protected, but a single fixed transform (e.g. hashing at ingestion) is a one-way
door: no role can ever see the real value, so it cannot serve legitimate privileged access
(fraud, support, compliance) and does not map to the masking primitives teams already use on
**Snowflake** (masking policies) and **Databricks** (column masks). We want **dynamic,
role-based masking**: the value is stored (protected at rest) and what each consumer sees is
decided **by their role, at read time** — and we want the *policy* to be portable, enforced
by whatever engine we run on (Postgres locally, Snowflake/Databricks in the cloud).

This follows the project's standing pattern: **govern once (ODCS), enforce per platform**
(ADR-0009 cloud portability, the ODCS→dbt source-of-truth).

## Decision

### 1. The masking policy is declared on the column in ODCS (source of truth)
Alongside `classification: pii`, a PII column declares a masking policy:

```yaml
- name: customer_document
  classification: pii
  customProperties:
    - property: maskingPolicy
      value:
        method: partial        # full | partial | hash | tokenize | null
        show_last: 4           # method-specific args
        unmask_roles: [compliance, fraud]   # roles that see the real value
```

The **policy** (what PII, how it's masked, who may see it unmasked) lives in the contract and
is portable. Default: only the owning domain role sees the raw value.

### 2. Enforcement is a per-platform adapter rendering the same policy
| Concern | Postgres (local) | Snowflake | Databricks |
|---|---|---|---|
| Tag the column as PII | — (from ODCS) | object tag | Unity Catalog tag |
| Apply the policy | a masking **view** (consumers) + base table (privileged roles), or a `mask()` SQL function keyed on the session role | native `CREATE MASKING POLICY` (role-aware) attached to the column | column `MASK` UDF (group-aware), + row filters |
| Who sees raw | role grants from `unmask_roles` | policy body checks `CURRENT_ROLE()` | UDF checks `is_account_group_member(...)` |
| At rest | pgcrypto / volume encryption | native encryption | native encryption |

Same contract → `GRANT`-gated masking views locally; a Snowflake masking policy or a Databricks
column mask in the cloud. The platform owns the adapter; the domain owns only the contract.

### 3. Masking vs. minimisation — when to use which
- **Dynamic masking (this ADR)** — the default for PII that *some* role legitimately needs
  unmasked. Reversible by role, portable, auditable.
- **One-way minimisation** (hash/tokenise at ingestion, ADR-0009 object layer) — only for
  values *no* role ever needs unmasked (e.g. a raw national id kept solely for dedup). Not
  role-based; cannot be undone.

### 4. Relationship to the output-port pattern
Today `customer.customers` masks by *transforming at the port* (`md5`), which is a static,
all-or-nothing mask. This ADR generalises it: the mask becomes a **policy on the column**,
role-aware and enforced by the engine, so the same published port shows the pseudonym to
marketing and the real value to compliance — without a second data product.

## Consequences
- Portable governance: the masking policy survives a move to Snowflake or Databricks; only the
  adapter changes.
- Needs a `platform/access` masking adapter (Postgres masking views + role grants now;
  documented Snowflake/Databricks equivalents) and a small ODCS convention for the policy.
- The local Postgres mechanism (views) is less ergonomic than native masking policies; it is a
  faithful stand-in, called out like ADR-0007/0011 where local ≠ prod mechanism.

## Implementation (M8)
`platform/access/apply_masking.py` reads each output port's ODCS `maskingPolicy` and creates a
role-aware view `<domain>_masked.<port>`:
`CASE WHEN pg_has_role(current_user, '<unmask_role>', 'MEMBER') THEN <raw> ELSE md5(<raw>) END`.
Two consumption planes: **end consumers** (BI/AI) connect as the least-privilege `analytics_reader`
and are granted the masking **view** (never the raw schema or the base table — denied); **internal
domain transformations** (cross-domain dbt-loom refs) read the base relation and re-govern any PII in
their own port. The `compliance` unmask role sees the raw value through the same view. **Proof P8**
(`make prove-p8`) asserts all three: consumer denied raw/staging, consumer sees `md5(raw)`, compliance
sees raw. On Snowflake/Databricks the same policy renders as a native masking policy / column mask —
only the adapter changes (ADR-0009).
