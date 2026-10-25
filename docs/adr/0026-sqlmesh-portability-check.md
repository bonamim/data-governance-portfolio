# ADR-0026: SQLMesh portability check — the contracts are the portable interface

**Status:** Withdrawn 2026-10-25 — the check, its CI step and proof P20 were removed; the record is
kept for its findings (the `sales_performance` integer cast it surfaced stays). **Relates to** [ADR-0006](0006-pipeline-state.md)
(pipeline state), [ADR-0009](0009-cloud-portability.md) (portability),
[ADR-0015](0015-scaling-the-transform-layer.md) (transform layer).

## Context
The platform's claim is that the ODCS contracts, not dbt, are the interface consumers rely on, so
the transform engine is replaceable. ADR-0006 also chose to keep pipeline state outside the engine
(ingestion ledger, Airflow, a dbt manifest in `platform-state`) where a natively stateful framework
keeps it inside. Neither claim had been tested against a second engine.

## Decision
Keep a **portability check**: the four output ports and their bronze inputs rebuilt natively in
**SQLMesh** (`portability/sqlmesh`, 0.236.3), from the same raw tables, in `sqlmesh_*` schemas,
as a least-privilege `sqlmesh_role` (reads raw, owns only its schemas). It is a check, not a second
production path: nothing consumes its output.

- **One source of truth.** `portability/sqlmesh/sync_contracts.py` generates each SQLMesh port
  model's `columns (...)` and `audits (...)` from its ODCS contract, as
  `platform/transform/sync_contracts.py` does for dbt; `make check` / CI fail on drift.
- **One judge.** `datacontract test` runs every port contract against both engines' tables.
- **Proof P20** (`make prove-p20`): no drift; SQLMesh builds; all 8 contract runs pass (4 ports ×
  2 engines); every port is identical row for row (`EXCEPT ALL` both ways); SQLMesh's interval state
  and restatement; its audit behaviour on a bad restated batch.

## Findings

| Topic | dbt (this platform) | SQLMesh | Consequence |
|---|---|---|---|
| Contracts | pass | pass | **The thesis holds**: same contracts, identical data on both engines |
| Physical types | `CREATE TABLE AS` takes the query's real types | Tables created from types it **infers** (`varchar`, `double`) | Declare the contract's types in the model (`columns`, generated); a contract with physical types catches it |
| A latent defect | `sales_performance` counts were `bigint`; the contract promises `integer` — unnoticed, since dbt's generated tests don't check physical types | same | **Fixed** (cast in the model). Found only because `datacontract test` judged both engines |
| State (ADR-0006) | Kept outside: ledger, Airflow, manifest in `platform-state`; windows computed by the runner (M9) | Inside the engine: built intervals per snapshot; a re-plan has nothing to do; `--restate-model` reprocesses a range | Both rewrite exactly a restated window (P6 / P20: 5,086 rows, 0 outside). SQLMesh needs no external state for this; ours stays engine-independent |
| Incremental by month | microbatch builds the current, partial month too | builds only **complete** intervals by default (`allow_partials` otherwise) | Same history; freshness of the open month differs |
| Write-audit-publish, code changes | per-model candidate + promote (M9) | virtual environments: new snapshot built aside, views swapped | Equivalent protection |
| Write-audit-publish, data restatement | the batch is audited **before** the merge (audit view, P15) | rows are written to the production table, **then** audited — a failing blocking audit fails the plan but consumers already see the rows (P20) | Not equivalent: on SQLMesh, restate in a development environment first, or keep a gate in front |
| Cross-domain refs | dbt-loom between per-domain projects | one project (plain model names) | A per-domain SQLMesh layout would need its multi-repo setup — not tested here |

## Consequences
- The engine is replaceable at the contract boundary, demonstrated rather than asserted; the check
  stays in CI (drift) and in the proof suite (data, behaviour).
- Contracts should carry precise physical types: they caught a defect only a second engine exposed.
- ADR-0006's choice stands: SQLMesh's built-in state is convenient, but our external state already
  delivers the same restatement guarantees engine-independently, and our audit-before-merge is
  stricter on data restatements.
