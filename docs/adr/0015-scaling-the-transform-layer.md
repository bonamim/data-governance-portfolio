# ADR-0015: Scaling the transform layer — Cosmos, per-model WAP, dbt state, microbatch backfill

**Status:** Proposed (design; implementation is a scoped build-out from M4 onward)
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

[astronomer-cosmos]: https://astronomer.github.io/astronomer-cosmos/
