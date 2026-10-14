# ADR-0019: Breaking-change management — gate, versioning, and consumer approval

**Status:** Accepted (gate + escape hatch implemented, proof P5; the GitHub approval automation is
deploy-time). **Relates to** [ADR-0012](0012-medallion-and-output-ports.md) (output ports),
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

### 3. Safe release — keep the old interface alive
The old version is not deleted on the bump: it stays as a **dbt model version** (`versions: [{v:1},
{v:2}]`, ADR-0015) and its ODCS contract is marked **deprecated** (`status` + a sunset date).
`v1` and `v2` coexist until consumers migrate; the deploy is therefore non-breaking. M4 lineage and
the data-product consumers tell you exactly whom to ask.

### 4. The automation (GitHub — optional convenience layer)
`.github/workflows/breaking-change-approval.yml` turns the approval into a button:

- on a breaking-change PR it **requests review** from the impacted consumer teams and comments who
  must approve;
- when an impacted consumer **approves** the PR, it **auto-commits** their entry into the approvals
  file (via `record_approval.py`), which re-runs the gate and unblocks the PR.

The native **Approve** is the button; the mapping convention is *GitHub team slug == domain name*.
This layer requires the repo on GitHub with those teams and a token allowed to push to PR branches —
until then it is inert, and the portable gate (§1–2) still enforces the policy.

## Consequences
- Output-port interfaces can't break silently; a breaking change is deliberate, **versioned**,
  **approved**, and **auditable**.
- The policy is portable (the gate + approval file work anywhere); GitHub is only automation on top.
- Consumers get a migration window (dbt model versions + deprecation), so no consumer is broken by a
  deploy — only by the scheduled retirement of the old version.
