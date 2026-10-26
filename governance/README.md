# `governance/` — the rules, as code

Federated computational governance (ADR-0003): the governance guild sets the rules every domain
follows, and CI enforces them on every pull request. This folder is *what* the rules are; the
platform (`platform/`) is *how* they are enforced at run time.

| Folder | What it holds | Who changes it |
|---|---|---|
| [`domains/`](domains/) | the domain registry: each domain's owner, contact and alert route | the guild, when a domain joins |
| [`tags/`](tags/) | the governed tags (sensitivity, privacy category), their allowed values and the warehouse tag each becomes | the guild |
| [`ontology/`](ontology/) | the business ontology: one Apache Ossie document of entity types, value types and relationships (ADR-0027) | the guild |
| [`standards/`](standards/) | the standards a domain meets: [data products](standards/data-product.md), [breaking changes](standards/breaking-changes.md), [the ontology](standards/ontology.md), [AI agents](standards/ai-agent.md), [model metadata](standards/model-metadata.md) | the guild |
| [`policies/`](policies/) | the checks that enforce the standards, one script per policy | the guild |
| [`approvals/`](approvals/README.md) | consumers' sign-off on a breaking change or a retirement | written by the approval workflow |
| [`requests/`](requests/README.md) | business requests to govern a product (filed by the AI agents) | written by the agents |
| `src/dgp_governance/` | the library every tool reads these definitions through | the guild |

The meaning of each value type is not here: it lives with the domain that owns it, in
`domains/<domain>/contracts/definitions/`, and contract columns link to it.

## The checks

Each policy is a script that prints one line per violation and exits non-zero when a rule breaks.
`make check` runs them all, then the linter and the tests. The breaking-change gate needs a base
branch to compare with, so it runs on its own (`make check-breaking`); CI runs both.

| Check | The rule | Make target |
|---|---|---|
| `check_domains.py` | the registry matches the folders under `domains/` | `check-domains` |
| `check_boundaries.py` | the platform never imports a domain, and no domain imports another (ADR-0008) | `check-boundaries` |
| `check_contracts.py` | every landing dataset has a valid contract with rules the loader can run | `check-contracts` |
| `check_data_products.py` | every data product definition meets the standard | `check-data-products` |
| `check_output_ports.py` | every output port is contracted, built, defined and fully documented | `check-output-ports` |
| `check_port_versions.py` | a versioned port's lifecycle: deprecated, sunset, retired | `check-port-versions` |
| `check_breaking_changes.py` | a breaking change ships only with a major bump and every consumer's approval | `check-breaking` |
| `check_ontology.py` | the ontology is valid Ossie, and every column takes its meaning from one definition | `check-ontology` |
| `check_semantics.py` | semantic models build only on output-port columns, fully described | `check-semantics` |
| `check_ai_agents.py` | each domain agent is scoped, isolated, owned, and exposes no raw PII | `check-ai-agents` |
| `check_pinned_images.py` | every image and GitHub Action is pinned by digest or commit SHA | `check-pinned-images` |

`record_approval.py` is not a check: the approval workflow runs it to record a consumer's sign-off.
