# The self-serve platform

Organised by the three pillars of data governance, on top of the core they govern
([ADR-0008](../docs/adr/0008-monorepo-module-boundaries.md), *Amendment*).

| Folder | Question it answers | Capabilities |
|---|---|---|
| [`access/`](access/) | **Who may read what?** | role-based masking views, consumer grants, landing-zone access policies, the dbt-state bucket policy |
| [`quality/`](quality/README.md) | **Is the data right?** | the quality engine behind the landing contract gate (`dgp_platform_quality`) |
| [`context/`](context/) | **What does it mean, where does it come from?** | [`catalog/`](context/catalog/README.md) (DataHub: deployment, recipes, governance and quality publishing), [`semantics/`](context/semantics/README.md) (Apache Ossie: reader, bundle, Superset publisher, Visualizer) |
| [`core/`](core/) | **The platform the pillars govern** | [`contracts/`](core/contracts/README.md) (the one ODCS reader), [`ingestion/`](core/ingestion/README.md), `transform/` (dbt), [`orchestration/`](core/orchestration/README.md) (Airflow), `infra/`, `recovery/` |

Contracts sit in the core, not in a pillar, because every pillar reads them: access takes the
masking policies, quality the rules, context the meaning. The rules a domain *must* follow are in
[`governance/`](../governance/) (the what); this folder is how they are enforced (the how).

## Where a file goes

- **A generator lives with the tool that reads its output** — not with its input, since nearly
  every generator reads the contracts. `transform/generate_dbt_ports.py` writes dbt YAML, so it is
  in `core/transform`; `generate_landing_access.py` writes MinIO policies, so it is in `access`.
- **Code a domain would otherwise copy** is a platform capability; a domain ships configuration
  only (ADR-0008 §2).
- **Third-party code** is vendored only when we import it and there is no release to pin
  (`context/semantics/vendor/ossie/`); a third-party *application* is built from a pinned upstream
  commit instead (`context/semantics/visualizer/`).

## Naming

| What | Convention | Examples |
|---|---|---|
| Folders | one lowercase word, or `snake_case` | `access`, `semantics`, `data_products`, `semantic_mcp` |
| Python modules, scripts | `snake_case.py` | `apply_masking.py`, `odcs.py` |
| Packages | `dgp_platform_<capability>` (distribution `dgp-platform-<capability>`) | `dgp_platform_contracts` |
| Tests | `tests/test_<subject>.py` next to the code | `tests/test_generate_dbt_ports.py` |
| Markdown | `kebab-case.md`; `README.md` per capability | `breaking-changes.md` |
| YAML | `.yaml`, except names a tool fixes | `dbt_project.yml`, `docker-compose.<tool>.yml` |
| Compose files | `docker-compose.<tool>.yml`, with an explicit `name:` | `docker-compose.visualizer.yml` |

**Scripts that produce something are named by where the output goes**, verb first — the verb says
whether it is reviewed in git, deployed as a file, or changes a running system
([docs/source-compile-deploy.md](../docs/source-compile-deploy.md)):

| Verb | Output | Committed? | Drift check | Scripts |
|---|---|---|---|---|
| `generate_` | files another tool reads inside the repository | yes | `--check` / CI diff | `generate_dbt_ports.py`, `generate_landing_access.py` |
| `build_` | a deployable artefact in `build/` | no | builds and validates | `build_ossie.py` |
| `publish_` | metadata pushed to a running tool (catalog, BI) | no | the tool's own drift check | `publish_governance.py`, `publish_landing_quality.py`, `publish_superset.py` |
| `apply_` | access controls enforced in the warehouse | no | proof P8 | `apply_masking.py`, `apply_grants.py` |

`core/transform/publish.py` is not a generator: it publishes *data* (write-audit-publish).
