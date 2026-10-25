# ADR-0019: Breaking-change management — gate, versioning, and consumer approval

**Status:** Accepted (gate + escape hatch implemented, proof P5; versioned ports implemented,
proof P19 — see *Amendment*; the GitHub approval automation is deploy-time). **Relates to** [ADR-0012](0012-medallion-and-output-ports.md) (output ports),
[ADR-0015](0015-scaling-the-transform-layer.md) (dbt model versions).

## Context
Output ports are contracts other domains build on. A breaking change to a port's ODCS contract
(removing/renaming/narrowing a field) silently breaks its consumers. M5 needs to **detect** such a
change, **block** it, and give a **governed path** to ship it when it's genuinely needed — proof P5:
*"a PR removing a column fails CI, lists the affected consumer, and passes only with a major version
bump + consumer approval."*

## Decision

### 1. The gate (CI, portable — the policy)
`governance/policies/check_breaking_changes.py` diffs every changed output-port contract against the
base branch with `datacontract breaking`. An ERROR-severity change **fails the gate** and names the
**affected consumers** — each port's declared consumers plus any domain that reads it as a
cross-domain input. Runs as `make check-breaking`; CI runs it on every PR. It has no GitHub
dependency — it works locally and in any CI.

### 2. The escape hatch — major version bump **plus** consumer approval
A breaking change passes only when **both** hold:

- the contract's **major version is bumped** (a declared, intentional break); and
- **every affected consumer has approved**, recorded in `governance/approvals/<port>-v<N>.yaml`.

**The approval file is the authority.** It is reviewable, versioned git history — a durable record of
who approved which breaking change, when. (The gate blocks with a clear reason at each missing step:
"needs a major version bump", then "needs approval from: …".)

### 3. Migration — coordinated by default, versioned only when a consumer can't cut over atomically
Approval is *consent*; the consumer still has to migrate. How depends on whether it can cut over at
the same moment the producer deploys:

- **Coordinated migration (the default).** Mesh consumers are dbt models in the same repo and CI, so
  the producer updates them in the same (or a fast-follow) PR; approval covers the coordinated change
  and the cutover is atomic. **No versioning needed — this is the common case**, and it is why most
  internal output ports never carry versions.
- **dbt model versions (only when needed).** For consumers that *cannot* cut over atomically —
  external teams on their own release trains, BI dashboards, or ad-hoc SQL reading the port directly —
  keep the old interface alive as a **dbt model version** (`versions: [{v:1},{v:2}]`, ADR-0015) with
  the old ODCS contract **deprecated** (`status` + sunset date). `v1` and `v2` coexist until those
  consumers migrate. This is opt-in *per port*; build it when such a consumer actually exists.
  *(Built as one port per major — `<port>_v<N>` — not dbt's `versions:`; see the Amendment.)*

M4 lineage + the data-product consumers tell you which case you are in and whom to ask.

### 4. The automation (GitHub — optional convenience layer)
`.github/workflows/breaking-change-approval.yml` turns the approval into a button:

- on a breaking-change PR it **requests review** from the impacted consumer teams and comments who
  must approve;
- when an impacted consumer **approves** the PR, it **auto-commits** their entry into the approvals
  file (via `record_approval.py`), which re-runs the gate and unblocks the PR.

The native **Approve** is the button; the mapping convention is *GitHub team slug == domain name*.
This layer requires the repo on GitHub with those teams and a token allowed to push to PR branches —
until then it is inert, and the portable gate (§1–2) still enforces the policy.

## Amendment (2026-10-19): versioned ports as explicit version models
The how-to, with a worked example of both paths:
[`governance/standards/breaking-changes.md`](../../governance/standards/breaking-changes.md).

§3's "dbt model versions" is built as **one port per major**, not dbt's `versions:` block:

- `v2` of `customers` is the port **`customers_v2`**: its own ODCS contract (major 2), dbt model and
  data product definition with `version_of: customers`. `customers` (v1) keeps its contract, model
  and **relation** untouched, so BI, ad-hoc SQL and dbt consumers on it see no change at release.
- **Why not dbt `versions:`.** An unpinned `ref('dgp_customer', 'customers')` — dbt-loom's
  cross-project refs included — resolves to the *latest* version, so releasing v2 would silently
  move every unpinned consumer onto the breaking shape: exactly what the gate exists to prevent.
  It would also make v1 and v2 share one contract-to-model sync, where the platform's tooling
  (sync, grants, masking, catalog, policies) is one contract per port.
- **The window.** Releasing a successor requires the old version to be `status: deprecated` with a
  `sunsetDate` (`check-port-versions`, in CI). The sunset becomes dbt's `deprecation_date` (dbt
  warns models that still ref it) and a DataHub `Deprecation` with the successor named. After the
  sunset, CI fails, naming the data products still reading it.
- **Retirement** is deleting the old contract. The gate blocks it while any data product still
  reads it, then requires the retired port's declared consumers in
  `governance/approvals/<port>-retire.yaml`.
- Releasing `customers_v2` is **not** a breaking change for the gate — nothing a consumer reads has
  changed — so it needs no approval; consent is collected at retirement, when it matters.

## Consequences
- Output-port interfaces can't break silently; a breaking change is deliberate, **approved**, and
  **auditable** (and, where needed, **versioned**).
- The policy is portable (the gate + approval file work anywhere); GitHub is only automation on top.
- Internal consumers migrate atomically alongside the approved change — no versioning overhead. Only
  consumers that can't cut over atomically (external / BI / ad-hoc) need the versioned-port
  migration window, opt-in per port — so the versioned-port machinery is built only when a real
  decoupled consumer appears, not up front.
