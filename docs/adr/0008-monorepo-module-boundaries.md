# ADR-0008: Monorepo with enforced module boundaries

**Status:** Accepted (§4 layout amended 2026-10-25, §4 packaging amended 2026-10-26 — see *Amendments*)

## Context
The whole platform lives in one repository. A monorepo keeps contracts, domains, platform
and governance reviewable together and versioned as one history. The risk is that a monorepo
quietly becomes a **distributed monolith**: domains import each other's code, the platform
reaches into a domain for a quick fix, and the data mesh ownership model (ADR-0003) exists
only in folder names. Boundaries that are not enforced do not hold.

## Decision
Keep the monorepo, and make the boundaries **executable**, not conventional.

### 1. Ownership — `CODEOWNERS`
Each top-level area has an owner. A change to a domain cannot be merged by another domain,
and platform/governance changes need their respective owners.

| Path | Owner |
|---|---|
| `platform/**` | platform team |
| `governance/**` | governance guild |
| `domains/<domain>/**` | that domain's team |

### 2. Code vs. configuration
The platform ships **reusable code**; a domain ships **declarative configuration only**:
source config, ODCS contracts, a dbt project, a data-product definition, and a DAG bundle
that wires platform operators. A domain team should not need to write platform code to
onboard — this is the self-serve platform of [data-mesh.md](../data-mesh.md) section 3.

### 3. Dependency direction — enforced by a boundary check in CI
| Rule | Allowed? |
|---|---|
| `domains/* → platform/*`, `domains/* → governance/*` | ✅ |
| `platform/* → domains/*` (platform depends on a domain) | ❌ |
| `domains/A → domains/B` (one domain imports another's code) | ❌ |

Domains integrate **only through published data products and contracts** — never by importing
code. The check (`governance/policies/check_boundaries.py`, `make check-boundaries`) reads every
file's imports with Python's `ast` and runs in CI; a violating PR fails.

### 4. Independent build and deploy units
- A **uv workspace** with one package per platform capability (`platform/<cap>`), each with its
  own `pyproject.toml` and version. Capabilities are released independently.
- **One dbt project per domain**; cross-domain `ref()` goes through dbt-loom and resolves to
  **public models only** (output ports), mirroring the Postgres output-port grants.
- **One Airflow DAG bundle per domain**, domain tasks running in their own container image
  (ADR-0005), so a domain deploys and fails independently (proof P1).

## Monorepo now, polyrepo when it pays off
A single repository is the right choice here: one maintainer, and the whole mesh is
reviewable and demonstrable in one place. Splitting into one repository per domain buys
**team autonomy** (separate access control, CI/CD and release cadence) — a benefit only
when separate teams own separate domains. Until then it adds cross-repo PR friction and
duplicated CI for no isolation gain.

Split a domain into its own repository when **any** of these becomes true:
- a distinct team takes ownership and needs its own merge rights and release cadence;
- the domain's CI needs to run independently of the rest (time or cost);
- access to the domain's code or data must be restricted from other domains.

Because of the boundaries above, the split is mechanical: the domain folder has no inbound
code dependencies, so it moves out with its CODEOWNERS entry, its package(s) and its dbt
project intact, consuming the platform as a published dependency.

## Consequences
- More packages and config than a single app — the cost of real isolation, and the point of
  the demonstration.
- The boundary rules are testable and reviewable, so governance is enforced by CI rather than
  by meetings (federated computational governance, ADR-0003).
- The repository can start as a monorepo and graduate domains to their own repositories
  without rework when a real team takes ownership.

## Amendments

### 2026-10-25: the platform is laid out by governance pillar
`platform/<capability>` became `platform/<group>/<capability>`, so the layout says what each
capability is for ([platform/README.md](../../platform/README.md)):

- **`access/`, `quality/`, `context/`** — the three pillars of data governance: who may read what,
  whether the data is right, and what it means and where it comes from (`context/catalog`,
  `context/semantics`).
- **`core/`** — the platform they govern: `contracts`, `ingestion`, `transform`, `orchestration`,
  `infra`, `recovery`.
- **The ODCS reader is its own capability, `core/contracts` (`dgp_platform_contracts`)**, split out of
  `quality`: every pillar reads contracts, so the reader cannot belong to one of them. `quality`
  keeps the engine that executes a contract's rules.

What does not change: CODEOWNERS (`/platform/` is one team), the dependency rules of §3 (a pillar
may import the core; nothing imports a domain), one package per capability, and the domain layout.
Scripts that produce something are named by where the output goes — `generate_`, `build_`,
`publish_`, `apply_` ([docs/source-compile-deploy.md](../source-compile-deploy.md)).

### 2026-10-26: every capability that others import is a package

§4 said "one package per platform capability", but only four were: the rest were scripts that
reached each other by inserting folders into `sys.path` (48 places), each with its own copy of the
helpers they shared — finding a contract, the repository root, a connection string, a Superset or
DataHub login. Now:

- **A capability whose code anything else imports is a workspace package**, with its own
  `pyproject.toml` and its dependencies declared: `dgp_governance` (the domain registry, tags,
  ontology, data products, output ports and approvals, read in one place), `dgp_platform_access`,
  `dgp_platform_semantics`, `dgp_platform_catalog` and `dgp_ai` join the four that already were.
- **A script imports packages, never another folder's script**, and nothing touches `sys.path`.
  `uv run` installs every member (they are all in the root `dev` group), so imports resolve by
  package name; a test of a script imports it through pytest's `pythonpath`. CI imports every
  script (`governance/tests/test_scripts_import.py`), so a broken import fails there rather than
  in the next restore drill.
- **The repository root has one definition** (`dgp_governance.repo.root()`): `DGP_REPO_ROOT`, as the
  Airflow image already set it, or the nearest folder upwards holding `governance/domains/`.

The dependency rules of §3 hold unchanged, and `check-boundaries` still enforces them. The
`governance/` package depends on the core's contract reader only; the policy checks stay scripts in
`governance/policies/`, since they depend on nearly every package and nothing depends on them.
