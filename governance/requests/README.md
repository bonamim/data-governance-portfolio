# Semantic model requests

Business requests to **govern a data product** — i.e. to add an Apache Ossie semantic model
(metrics + dimensions, ADR-0010) to a product that doesn't have one yet.

The governed AI (`dgp_ai.semantic_server`, M7) will not fabricate a metric that isn't defined. When a user
explores the catalog and wants a number on an *ungoverned* product, the agent's
`request_semantic_model` tool drops an auditable request here, routed to the owning domain — the
self-serve path from discovery to a governed metric. Each file is one open request, named
`<domain>.<product>.<UTC timestamp>.yaml` so a second request never overwrites an open one:

```yaml
data_product: orders
owner_domain: sales
owner_contact: "#sales-data"
business_need: "Monthly order volume and revenue by state for campaign planning"
requested_by: jbonamim
requested_at: "2026-10-15T10:26:11Z"
status: open
```

**The owner is told at filing time.** The semantic server posts the request to the domain's alert
route from `governance/domains/<domain>.yaml` (`alerts.webhook_env`, the same Slack-compatible
webhook as landing-zone alerts, ADR-0016) with `kind: semantic_model_request`. The tool's result
carries `owner_notification` (`notified`, `channel`, and a `reason` when it wasn't sent: no webhook
configured, or the webhook failed), so the agent reports what actually happened. A failed
notification never loses the request; the file is written first.

The owning domain authors `domains/<domain>/semantics/<product>.semantic_model.yaml` to satisfy the
request (CI's `check-semantics` keeps it on the contract's columns, or fields derived only from them), then resolves/removes the file.
This mirrors `governance/approvals/` for breaking changes: governance decisions live in git, not in
a tool's UI.
