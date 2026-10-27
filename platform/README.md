# The self-serve platform

The platform is laid out by the three pillars of data governance, on top of the core they govern
([ADR-0008](../docs/adr/0008-monorepo-module-boundaries.md), *Amendments*).

| Folder | The question it answers | What is in it |
|---|---|---|
| [`access/`](access/) | **Who may read what?** | role-based masking views, consumer grants, landing-zone access policies, the dbt-state bucket policy |
| [`quality/`](quality/README.md) | **Is the data right?** | the quality engine behind the landing contract gate |
| [`context/`](context/) | **What does it mean, and where does it come from?** | [`catalog/`](context/catalog/README.md) (DataHub: deployment, recipes, governance and quality publishing) and [`semantics/`](context/semantics/README.md) (Apache Ossie: the reader, the bundle, the Superset publisher, the Visualizer) |
| [`core/`](core/) | **The platform the pillars govern** | [`contracts/`](core/contracts/README.md) (the one ODCS reader), [`ingestion/`](core/ingestion/README.md), `transform/` (dbt), [`orchestration/`](core/orchestration/README.md) (Airflow), `infra/`, `recovery/` |

Contracts live in the core, not in a pillar, because every pillar reads them: access takes the
masking policies from them, quality the rules, context the meaning. The rules a domain *must*
follow are in [`governance/`](../governance/) (the what). This folder is how they are enforced
(the how).

## Packages and scripts

The repository is a [uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/). A
capability whose code anything else imports is a **package**: its own `pyproject.toml`, its code
under `src/<package>/`, its dependencies declared. Everything else is a **script**: an entry point
that imports packages, and never another folder's script.

| Package | Folder | What other code takes from it |
|---|---|---|
| `dgp_governance` | `governance/` | the domain registry, tags, ontology, data products, output ports, approvals |
| `dgp_platform_contracts` | `core/contracts/` | `odcs.load`, the one way to read a contract |
| `dgp_platform_quality` | `quality/` | the quality engine and the landing contract rules |
| `dgp_platform_ingestion` | `core/ingestion/` | the landing layout, the ledger, connection settings |
| `dgp_platform_orchestration` | `core/orchestration/` | the DAG factories the domains' bundles call |
| `dgp_platform_access` | `access/` | the grants plan (proof P11 checks the warehouse against it) |
| `dgp_platform_semantics` | `context/semantics/` | the Ossie reader and Ossie's own validator, a Superset client |
| `dgp_platform_catalog` | `context/catalog/` | the DataHub publishers and a small GMS client |
| `dgp_ai` | `../ai/` | the governed semantic layer, the agent registry, the router |

Two consequences keep this honest:

- **No path tricks.** Nothing inserts folders into `sys.path`. `uv run` installs every member, so
  a script or a test imports what it needs by package name. (A test of a script imports it through
  pytest's `pythonpath` setting, in `pyproject.toml`, and `test_scripts_import.py` imports every
  script in CI, so a broken import can't wait for the script's next real run.)
- **One home per fact.** Where a contract lives, which product reads a port, how to reach the
  warehouse: each is answered once, in a package, and every tool asks it.

The packaged tools run as modules (`uv run python -m dgp_platform_access.apply_grants`), and the
Makefile wraps each one in a target (`make grants`).

The repository root is found the way `git` finds it: `DGP_REPO_ROOT` if set (the Airflow image
mounts the checkout at `/opt/airflow/repo`), otherwise the nearest folder upwards that holds
`governance/domains/`.

## Where a file goes

- **A generator lives with the tool that reads its output**, not with its input, since nearly every
  generator reads the contracts. `transform/generate_dbt_ports.py` writes dbt YAML, so it is in
  `core/transform`, and `generate_landing_access` writes MinIO policies, so it is in `access`.
- **Code a domain would otherwise copy** is a platform capability, and a domain ships configuration
  only (ADR-0008 §2).
- **Third-party code** is vendored only when we import it and there is no release to pin
  (Ossie's validator, in `dgp_platform_semantics/vendor/ossie/`). A third-party *application* is
  built from a pinned upstream commit instead (`context/semantics/visualizer/`).

## Naming

| What | Convention | Examples |
|---|---|---|
| Folders | one lowercase word, or `snake_case` | `access`, `semantics`, `data_products` |
| Python modules and scripts | `snake_case.py` | `apply_masking.py`, `odcs.py` |
| Packages | `dgp_platform_<capability>` (distribution `dgp-platform-<capability>`), with `dgp_governance` and `dgp_ai` outside the platform | `dgp_platform_contracts` |
| Tests | `tests/test_<subject>.py` next to the code | `tests/test_generate_dbt_ports.py` |
| Markdown | `kebab-case.md`, and a `README.md` per capability | `breaking-changes.md` |
| YAML | `.yaml`, except where a tool fixes the name | `dbt_project.yml`, `docker-compose.<tool>.yml` |
| Compose files | `docker-compose.<tool>.yml`, with an explicit `name:` | `docker-compose.visualizer.yml` |

**A tool that produces something is named after where the output goes**, verb first. The verb says
whether the output is reviewed in git, deployed as a file, or changes a running system
([docs/source-compile-deploy.md](../docs/source-compile-deploy.md)):

| Verb | Output | Committed? | Drift check | Tools |
|---|---|---|---|---|
| `generate_` | files another tool reads, inside the repository | yes | `--check` / CI diff | `generate_dbt_ports`, `generate_landing_access` |
| `build_` | a deployable artefact in `build/` | no | builds and validates | `build_ossie` |
| `publish_` | metadata pushed to a running tool (catalog, BI) | no | the tool's own drift check | `publish_governance`, `publish_landing_quality`, `publish_superset` |
| `apply_` | access controls enforced in the warehouse | no | proof P8 | `apply_masking`, `apply_grants` |

`core/transform/publish.py` is not one of these: it publishes *data* (write-audit-publish).
