# Access: who may read what (`dgp_platform_access`)

Access is declared in the governed definitions and enforced by the platform, never granted by hand:

| Tool | What it does | Declared in | Run |
|---|---|---|---|
| `apply_grants` | gives each consumer exactly the output ports its data products name (output ports only, through the masking view where a port carries PII) | `domains/*/data_products/*.yaml` | `make grants` |
| `apply_masking` | creates a role-aware view per port that masks PII: masked for consumers, raw for an unmask role such as `compliance` (ADR-0013) | each column's `maskingPolicy` in its ODCS contract | `make mask` |
| `generate_landing_access` | writes the landing zone's object-store policies, and the production bucket policies, KMS keys and secrets inventory (ADR-0024) | `domains/*/ingestion/sources.yml` | `make landing-access` |

```
platform/access/
  src/dgp_platform_access/   the three tools above
  landing/                   generated landing-zone policies (committed; CI fails if they drift)
  state/                     the dbt-state bucket policy
  tests/
```

The warehouse tools run as `access_admin`, never the superuser: it belongs to every domain role, so
it may grant on their ports, but it cannot create roles (X5). dbt's rebuilds drop views and grants,
so the transform DAG and `make publish` re-apply masking, then grants, after every domain. Proof P8
(`make prove-p8`) checks the result on the live warehouse, and P11 checks each domain agent's role.
