# Breaking-change approvals

When an output port ships a **breaking change** (a major version bump on its ODCS contract), every
affected consumer must sign off. Approvals are recorded here, one file per port + major version:
`<port>-v<N>.yaml`.

**The file is the authority.** The breaking-change gate
(`governance/policies/check_breaking_changes.py`, proof P5) lets a breaking change pass only when
every affected consumer appears here. The GitHub approval workflow writes these files automatically
when consumers approve the PR (ADR-0019); they can also be committed by hand.

```yaml
port: customers        # the output port
version: 2             # the new major version this approval is for
approved_by:
  - team: sales        # an affected consuming team (must match the data-product consumer/input)
    user: jane         # who approved (optional)
    at: 2026-10-14     # when (optional)
    pr: 42             # the PR (optional)
```

A breaking change is unblocked only once `approved_by` covers **all** affected consumers — their
declared consumers plus any domain that reads the port as a cross-domain input. The old version
stays alive (dbt model version `v{N-1}`) with a deprecation window until consumers migrate.
