# ADR-0001: dbt Core for transformation

**Status:** Accepted

## Context
The warehouse layer needs SQL transformations with tests, documentation and lineage
that the catalog and the contract tooling can read. Candidates: dbt Core, SQLMesh.

## Decision
Use **dbt Core**, one dbt project per domain, with **dbt-loom** for cross-domain `ref()`
to public models only.

## Consequences
- Native DataHub ingestion of models, tests and column-level lineage.
- datacontract-cli can import from and export to dbt.
- Cross-project references in dbt Core require the dbt-loom plugin (dbt Cloud's
  native multi-project feature is not open source).
- SQLMesh offers stronger built-in breaking-change detection; here that role is
  covered by the contract gate in CI.
