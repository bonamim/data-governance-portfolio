# Breaking-change approvals

When an output port ships a **breaking change** (a major version bump on its ODCS contract), every
affected consumer must sign off. Approvals are recorded here, one file per port + major version:
`<port>-v<N>.yaml`.

This page is the record's format. **How** to ship the change (a coordinated migration in one PR,
or a versioned port beside the old one), with worked examples, is
[`governance/standards/breaking-changes.md`](../standards/breaking-changes.md).

**The file is the authority.** The breaking-change gate
(`governance/policies/check_breaking_changes.py`, proof P5) lets a breaking change pass only when
every affected consumer appears here. The GitHub approval workflow writes these files automatically
when consumers approve the PR (ADR-0019). They can also be committed by hand.

```yaml
port: customers        # the output port
version: 2             # the new major version this approval is for
approved_by:
  - team: sales        # an affected consuming team (must match the data-product consumer/input)
    user: jane         # who approved (optional)
    at: 2026-10-14     # when (optional)
    pr: 42             # the PR (optional)
```

A breaking change is unblocked only once `approved_by` covers **all** affected consumers: their
declared consumers plus any domain that reads the port as a cross-domain input.

**Retiring a port version** (deleting `<port>.odcs.yaml` after its successor `<port>_v<N>` took
over, ADR-0019 §3) is breaking too. The gate blocks it while any data product still reads it, then
needs the retired port's declared consumers in `<port>-retire.yaml`:

```yaml
port: customers
change: retire v1 (successor customers_v2)
approved_by:
  - team: sales
```
