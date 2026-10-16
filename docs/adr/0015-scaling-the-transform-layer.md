# ADR-0015: Scaling the transform layer — Cosmos, per-model WAP, dbt state, microbatch backfill

**Status:** Accepted — implemented in M9 (2026-10-16); see *Implementation* below for what was built and how it differs in detail
**Extends** [ADR-0005](0005-airflow-orchestration.md) (Airflow), [ADR-0006](0006-pipeline-state.md) (microbatch).
**Supersedes the *mechanism* of** [ADR-0014](0014-flexible-write-audit-publish.md) — the opt-in /
severity policy stands; how it is built and orchestrated changes here.

## Context
The first transform layer is a correct PoC but will not survive production as domains grow from
1–2 models to 20–30 large ones. Four problems, all visible in today's code:

1. **WAP full-rebuilds and only covers full tables.** `wap_alias` gates the candidate path on
   `node.config.materialized == "table"`, so the *big* models — the ones that will be
   incremental — get **no** audit gate. Every protected port is rebuilt from scratch each run.
2. **The swap is unsafe at scale.** `publish.py` does `DROP TABLE … CASCADE` + `RENAME`, which
   silently drops dependent objects (downstream views, FKs) and all grants.
3. **One opaque task per domain.** A single `publish` task shells out to `publish.py`; a failure
   could be build, audit, swap, or grant, with no per-model retry, parallelism, or lineage.
4. **No state and no backfill.** Every run is a full `dbt build` of the whole project; there is
   no persisted manifest, and asset-scheduled DAGs (`catchup=False`, no data interval) have no
   way to reprocess a time window.

These are performance, observability, maintenance, and operability gaps — the exact things a
"PoC shaped for production" has to answer.

## Decision

### 1. Orchestrate dbt with Cosmos, in **manifest mode**
Render each domain's dbt project as a per-model task graph (build + test per node) with
[astronomer-cosmos], replacing the single `publish` task. This buys per-model **retries**,
**parallelism**, **selective runs**, and lineage in the Airflow UI.

- **`LoadMode.DBT_MANIFEST`, never `DBT_LS`.** The `manifest.json` is generated once at image
  build / deploy time and Cosmos renders from it. Running `dbt ls` at every DAG parse would run
  dbt on the scheduler on every parse cycle and does not scale — this is non-negotiable past a
  handful of models.
- **Parameterize the DAG factory.** `build_transform_dag(domain, *, start_date, catchup,
  retries, retry_delay, tags, default_args, params)` with today's values as defaults, so the
  platform ships paved defaults and a domain overrides only what it needs (self-serve platform,
  federated governance — ADR-0003/0008).

### 2. WAP becomes **per-model, materialization-aware, and portable**
The ADR-0014 policy (opt-in via `publish.audit`, severity-aware) is unchanged. The mechanism
moves from a monolithic per-domain rename to a per-model *promote*, compiled to each engine's
cheap primitive:

| Engine | Promote primitive |
|---|---|
| Postgres (PoC store) | build into a per-domain **candidate schema**, metadata **rename** on pass |
| Snowflake / Databricks / BigQuery | **zero-copy clone** swap (near-free) |
| Iceberg / Delta | **branch** + cherry-pick / fast-forward |

The promote runs **inside the dbt project** — it is a `dbt run-operation` macro in the `dgp_dbt`
package (`wap_publish` / `wap_drop_candidates`), not a hand-rolled SQL script. `dbt build` is the
*write + audit*; a thin orchestrator (`publish.py` / the Airflow task) only sequences
`dbt build → dbt run-operation`. This is the community-standard shape (dbt's own guidance runs the
swap as `dbt run-operation`/`dbt clone`, never as external SQL) and keeps the promote portable —
the same macro name maps to `ALTER SCHEMA … SWAP` / `dbt clone` on a warehouse. *(Implemented
2026-10-13.)*

Clarifications that resolve the review questions:
- The candidate schema is **one staging namespace per domain** (e.g. `sales_gold__candidate`),
  not one per output port. A schema is metadata — free to create/drop. **It is not the
  bottleneck;** the full *rebuild* is, which is why big models go incremental.
- **No blanket `DROP … CASCADE`.** Promote must preserve dependents and grants (rebuild
  dependent views, re-apply grants deterministically), not cascade them away.
- Per model: **table** → build candidate → audit → swap; **incremental / microbatch** → audit
  the *batch* in staging → `merge` / replace partition on pass; **view** → publish in place.
- **Per-model promote, not domain-atomic, by default.** At 30 models one bad model should not
  block 29 good ones. A product that genuinely needs all-or-nothing keeps a **schema-level
  swap** gated on the whole TaskGroup — declared, not the default.

### 3. dbt **state + defer** to skip redundant *code* work
State is the **code-change** axis and is complementary to incremental (the **data-change** axis);
"skip redundant model executions" needs both.

- Persist `target/manifest.json` from each successful run to the **`platform-state` MinIO
  bucket**; the next run pulls it as `--state`. Missing/stale → safe fallback to a full build.
  The same artifact is Cosmos's manifest (§1).
- **Slim CI:** `dbt build --select state:modified+ --defer` — a PR builds only the models it
  changed (and descendants) and defers the rest to the published environment.
- `--defer` also makes **per-model WAP cheap**: build only the changed models into the candidate
  schema and `ref()` the *published* versions of everything else — build 2 models, not 30.

### 4. Backfill = **dbt microbatch** + runtime **DAG params**, never a cloned DAG
- Time-series models use the dbt 1.9 **`microbatch`** strategy (`event_time`, `batch_size`,
  `lookback`). Each batch is idempotent and overwrites *its* partition, so reprocessing is safe
  and parallelizable (ADR-0006).
- Reprocess a window by **triggering the same DAG with `params`** (`event_time_start` /
  `event_time_end`), which flow into `dbt --event-time-start/--end`. A normal run leaves them
  null. **No per-pipeline backfill DAG** — that duplicates logic and drifts; at most one generic
  entrypoint that calls the *same* task code. (Airflow-native `catchup` backfill does not apply —
  our DAGs are asset-scheduled with no data interval.)
- Guardrails: idempotent partition overwrite (never append on backfill), a concurrency cap so a
  long window does not launch hundreds of parallel batches, and **a backfilled batch is audited
  before it is merged** (microbatch + audit-the-batch is the incremental form of WAP, §2).

## Consequences
- The transform layer scales on all four axes: per-model observability/retry (Cosmos), bounded
  compute (incremental + microbatch + `--defer`), safe promotes (no cascade), and first-class
  backfill (params + microbatch).
- New operational dependencies: a generated-and-published dbt manifest, the `platform-state`
  bucket as the state store, and Cosmos in the Airflow image.
- Portability holds (ADR-0009, ADR-0013): WAP is a declared policy that maps to clone/branch on
  the warehouses, not Postgres-specific plumbing.
- Build-out is incremental and can land per concern: factory params first (cheap), then Cosmos
  on one domain, then the per-model promote, then state/defer, then microbatch backfill.

## Implementation (M9, 2026-10-16)

All four decisions are built and proven on the live platform (P6, P15, P16; P3 now holds in
Airflow too). What was built, and the details the design left open:

**One runner, two entry points.** `dgp_platform_orchestration.wap` holds the plan (which models
opt in, upstream-first order, batch windows, descendants) and is used by both `publish.py` (CLI,
restore drill, proofs) and the Airflow DAG, so the two cannot drift. The publish SQL stays in the
`dgp_dbt` macros (`wap_prepare`, `wap_audit_view`, `wap_promote`, `wap_drop_candidate`).

**Cosmos (§1).** `astronomer-cosmos==1.15.1`, `LoadMode.DBT_MANIFEST`, `ExecutionMode.LOCAL`,
`TestBehavior.AFTER_EACH`. The manifest comes from `make dbt-manifests` (run by `make airflow-up`;
the deploy step in production). The DAG per domain is:
`models` (one run + test group per model, `package:<project>` so dbt-loom's injected upstream
models are not rendered) → `wap_<model>` (`plan → stage → write → audit_view → audit → publish`,
`quarantine` on failure, audit never retried) → `downstream` → `grant` (always) → `save_state`
(only when everything published; the only task emitting `published/<domain>`). Edges are
per model (Cosmos `tasks_map`), not per group. Cosmos runs dbt from a temporary copy of the
project, so the shared package path (`packages.yml`) and the dbt-loom manifest paths take an
absolute prefix from env vars that only Airflow sets. Cosmos emits no Airflow datasets, keeping
ADR-0018's rule that dbt owns transform lineage.

**Per-model promote on Postgres (§2).** The candidate is `<name>__candidate` in the model's own
schema (an alias, not a candidate schema: one fewer schema per domain to own and grant). The audit
runs the model's dbt tests against `<name>__audit`, a view of the table *as consumers would get it
after the promote*. The promote:

| Case | Promote |
|---|---|
| table (or incremental rebuilt in full), same columns | rows replaced **in place** (truncate + insert, one transaction): same table, so dependent views, grants, masking and docs survive |
| first publish, a view becoming a table, or a new shape | rename the candidate in; **refused** while anything depends on the old relation (never `CASCADE`) |
| microbatch with a window | the candidate holds only the window's batches; merge = delete the window + insert, one transaction |

In-place replacement costs a copy on Postgres; on a warehouse the same macro maps to a zero-copy
clone swap. A WAP model downstream of another runs after its publish and is skipped if that one
was quarantined; a non-WAP descendant of a quarantined model is not rebuilt.

**Audit the batch as the table (§2, §4).** Auditing only the batch would miss table-level rules: the
`rowCount` floor fails on a one-month batch, and a batch that is clean on its own can duplicate an
order elsewhere in the table. The audit view (published rows outside the window + the new batches)
makes every contract rule judge the post-merge table, at the cost of a view. P15 proves both cases.

**State and Slim CI (§3).** Every successful publish saves `target/manifest.json` to
`s3://platform-state/dbt/<domain>/manifest.json` as the `dbt-state` principal (read/write on
`dbt/*` only; the restore drill passes `--no-state`). `make slim-ci` plans
`state:modified+` against that baseline and builds it with `--defer --target ci` into the
domain's `<domain>_ci` sandbox schema (owned by the domain role, emptied first, never granted).
Pull requests run the same plan against the base branch, parsed without a warehouse
(`--baseline git:<ref> --plan-only`). The state store is regenerable: if it is lost, the next
publish recreates it and Slim CI reports "no baseline" until then.

**Backfill (§4).** `sales.orders` is the first microbatch port (monthly, `lookback=1`,
`unique_key` — dbt-postgres merges batches). A normal run audits and merges the current batch
plus the lookback; `make backfill DOMAIN=… START=… END=…` or the DAG's `event_time_start` /
`event_time_end` params reprocess exactly that window (aligned to whole batches) through the same
audit; `full_refresh` rebuilds in full. Guardrails: `max_active_runs=1` per transform DAG, and the
merge replaces the window (never appends).

**Known limits.** Backfill params apply to opted-in microbatch models; a non-microbatch
incremental model opted in to WAP is rebuilt in full into its candidate. In Airflow, the
`quarantine` task of a WAP model that was skipped because an upstream audit failed also runs
(trigger rule `one_failed` counts `upstream_failed`); it only drops a candidate that does not
exist.

[astronomer-cosmos]: https://astronomer.github.io/astronomer-cosmos/
