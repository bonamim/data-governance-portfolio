# Source → compile → validate → deploy

Every governed fact in this platform is written once, by the team that knows it, and everything
else is derived from it — the pattern dbt and Snowflake use for models and semantic views, applied
to contracts, meaning and access too.

## The rules

1. **One source per fact, in git, owned by the team that knows it.** Domains own their contracts,
   data elements, semantic models and data product definitions (their folders, their CODEOWNERS);
   the governance guild owns the ontology, the governed tags and the standards; the platform owns
   the generators.
2. **Everything else is generated**, by a platform generator that is idempotent and has a check
   mode (`--check`, or a drift diff in CI).
3. **A generated file is committed only when a tool needs it inside the repository** (dbt, the
   landing-zone policies) — and CI fails when it drifts. Anything deployed elsewhere is built
   into `build/` or applied to a live system from git; nothing is hand-edited there.
4. **One reader per standard.** ODCS through `dgp_platform_contracts.odcs` (links inlined), Apache
   Ossie through `platform/context/semantics/ossie.py` (load, compile, Ossie's own validator), governance
   through `governance/registry.py`. No generator parses a standard its own way.
5. **Generators read sources, not each other's output** — so there are no chains to break, and a
   new consumer is one new generator.

## The generators

| Source (git) | Generator | Output | Committed? | Checked by |
|---|---|---|---|---|
| ODCS contracts (+ linked data elements) | `make dbt-ports` | dbt `_ports.yml`: columns, descriptions, contract tests, governed-tag metadata | yes — dbt reads it | CI drift diff |
| `ingestion/sources.yml` | `make landing-access` | landing bucket and key policies | yes | `check-landing-access` |
| ontology, data elements, semantic models, contracts | `make ossie-build` | `build/ossie/`: the ontology, the compiled semantic models, the mappings — the bundle Ossie tools and Snowflake read | no — built | `check-ontology` (builds and validates) |
| port metadata from `dbt-ports` | the dbt build (post-hook) | governed tags on each output port, before access is granted | — warehouse | `governance_tags_preview` |
| contracts (masking policies), data product definitions | `make mask`, `make grants` | masking views, consumer grants | — warehouse | proof P8 |
| data products, contracts, ontology, tags | `make catalog-govern` | DataHub: domains, products, business glossary, data elements, tags | — catalog | re-applied from git (ADR-0018) |
| compiled semantic models | `make bi-publish` | Superset datasets and metrics | — BI | `bi-check-drift` |

Each script is named by where its output goes, which is this table's *Committed?* column:
`generate_` (committed files), `build_` (an artefact in `build/`), `publish_` (catalog, BI) and
`apply_` (warehouse access controls) — the convention is in [platform/README.md](../platform/README.md).

## Does it scale — or is it a monster?

**Not a monster.** Every fact has one source; a generator exists only because a tool cannot read
the standard files itself, and it goes away as soon as that tool can.

- **Big teams.** A domain changes only its own folder; a definition another domain reuses is linked,
  not copied; ownership is the folder plus CODEOWNERS. With a repository per domain, a domain
  vendors the definitions it reuses at the owner's path (`governance/tests/test_ontology_multirepo.py`).
- **Complex pipelines.** Transforms run per domain (an Airflow DAG each), per model (Cosmos tasks),
  with write-audit-publish and Slim CI; tags, masking and grants happen inside that order, so a new
  object is protected before it is readable.
- **Cost.** `make check` — every governance check and the test suite over the whole repository —
  runs offline in about 16 seconds; each check is linear in the files it reads, with no service.
- **What to watch.**
  - The number of generators is not a target, it is the list of tools that cannot read ODCS or
    Ossie yet. Of today's seven, six stay (dbt, landing policies, the Ossie bundle, the warehouse
    tags inside the dbt build, masking and grants, the DataHub governance layer); `bi-publish` goes away when Superset
    reads Ossie itself; DataHub's ODCS source could take over part of `catalog-govern`. A tool that
    already reads the standard (the Ossie Visualizer, Snowflake's create-from-Ossie) needs none.
  - Two standards are drafts. Every Ossie document is `version: 0.2.0.dev0`, validated against a
    pinned copy of Ossie's own schemas; Snowflake reads semantic views back as Ossie `0.1.1` by
    default and does not document which versions it creates from. The compile step is the one place
    to emit what a consumer needs — to verify on a real Snowflake account.
  - Committed generated files make diffs longer; that is dbt's requirement.
  - Checks run over the whole repository; scoping them to the domains a change touches is the next
    step if it ever grows slow.
