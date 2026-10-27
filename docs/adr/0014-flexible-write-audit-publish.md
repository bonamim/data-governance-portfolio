# ADR-0014: Flexible write-audit-publish: opt-in, severity-aware, materialization-aware

**Status:** Accepted (opt-in implemented, severity + incremental are scoped build-out)
**Supersedes** the write-audit-publish part of [ADR-0006](0006-pipeline-state.md).
**Superseded in part by** [ADR-0015](0015-scaling-the-transform-layer.md): the opt-in / severity
policy below stands, but the *mechanism* (per-domain candidate rename) is reshaped there into a
per-model, portable, state-aware promote.

## Context
Write-audit-publish (WAP) guarantees consumers never see a bad build. But the first cut made it
**mandatory for every full-refresh table port**, which is wrong on three counts:

1. **Not every product needs it.** WAP costs a double build / a swap, and most products are fine
   being published in place, with failures simply alerting.
2. **Not every DQ failure should block publishing.** A hard integrity breach must quarantine,
   while a soft, informational check should publish and flag. The gate must be **by severity**.
3. **It must be correct for every materialization**: full table, incremental (merge /
   delete+insert), view, snapshot, not just full-refresh tables.

## Decision

### 1. WAP is opt-in, per data product
A data product declares its publish policy in its definition:

```yaml
publish:
  audit: required   # required = write-audit-publish, off (default) = publish in place
```

Default is **off**: build in place, run the contract DQ, and *alert* on failure (the bad build
is visible but published, which is acceptable for most products). `required` turns on WAP. So a team
opts a *critical* product in, and everything else stays simple.

### 2. The audit gate is by severity: only blocking DQ quarantines
Contract DQ rules carry a **severity**:
- **blocking** (dbt `severity: error`): a failure **quarantines** the build (WAP) or **fails
  the run** (in-place). Use for integrity: PK uniqueness, not-null keys, referential rules.
- **warn** (dbt `severity: warn`): a failure **publishes anyway** and raises an assertion /
  alert. Use for soft expectations: freshness drift, distribution checks, completeness bands.

So "only critical DQ failing" blocks: WAP swaps unless a **blocking** rule fails. Warn-level
results ride along as visible assertions (DataHub at M4). Severity is declared on the ODCS
quality rule and carried into the generated dbt test.

### 3. The mechanism adapts to the materialization
| Materialization | How WAP works |
|---|---|
| **view** | No materialized data to protect, so it is published in place and the contract DQ runs live. (Materialize as a table if you need point-in-time protection.) |
| **table (full refresh)** | Build to `<name>__candidate`, run blocking DQ on it, **atomic swap** into the published name. *(Implemented, proof P3.)* |
| **incremental (merge / delete+insert)** | **Audit the batch, then merge.** Build the increment to a staging delta, run blocking DQ on the delta (and, where needed, on the post-merge preview in a transaction), and only `merge` / `delete+insert` into the target on pass. On a Postgres target this is a `BEGIN; apply; audit; COMMIT|ROLLBACK`, and consumers see the prior data until commit. |
| **incremental: `microbatch`** | **Per-partition audit.** dbt processes independent time-window batches (`event_time` + `batch_size`, ADR-0006). Each batch's rows are built to a staging relation, the blocking DQ runs on that batch, and only a passing batch replaces its partition in the target. A failing batch is **quarantined** (held/retried) while the rest publish, so one bad day never blocks the others (per-partition accuracy, not all-or-nothing). Needs a custom microbatch materialization that audits before the per-batch insert, or an orchestration-level shadow-then-merge. dbt has no native per-batch data gate. |
| **snapshot / materialized view** | Build the new version aside, audit, swap/refresh on pass. |

In every case: pass → publish, and blocking failure → leave the last good version, drop the
candidate/rollback, emit the failed assertion and alert.

## Consequences
- Proportional: critical products get the guarantee, and the rest stay cheap and simple.
- The audit is driven by **severity on the ODCS quality rules**, so the policy is declared, not
  coded.
- The incremental path (audit-batch-then-merge / transactional merge) is more work than the
  full-table swap and is the main remaining implementation. The view and full-table cases are
  covered.
- `platform/core/transform/publish.py` reads each product's `publish.audit` and only write-audit-
  publishes the opted-in table ports, and others build in place.
