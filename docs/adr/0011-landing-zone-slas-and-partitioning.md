# ADR-0011: Landing-zone SLAs, partitioning, retention, and shared-dataset ownership

**Status:** Accepted — §2 (partitioning) and §3 (retention) amended by
[ADR-0024](0024-landing-zone-layout-access-and-lifecycle.md): two buckets (inbox + object-locked
archive), partitions derived from the contract by the platform, retention per contract class.

## Context
The landing zone is a governed boundary (docs/data-mesh.md). Beyond schema and quality,
production needs operational guarantees — when does data arrive, how fresh is it, is it
complete, how long is it kept — and an answer to a sharp question: a landing dataset can be
consumed by several domains, so **who owns its contract, and how is it stored** without
implying it belongs to one consumer?

## Decision

### 1. SLAs and KPIs are declared in the ODCS contract (single source of truth)
Every landing dataset's ODCS contract declares, as first-class fields:

| KPI | ODCS field | Executed |
|---|---|---|
| Delivery timeliness (e.g. by 04:00 UTC) | `slaProperties: timeOfAvailability` | scheduled monitor (M4) |
| Data freshness (events up to end of previous day) | `slaProperties: latency` + `slaDefaultElement` | engine on arrival where there is an event-time column (M4 for time-based alerting) |
| Frequency (daily) | `slaProperties: frequency` | — |
| File completeness (min rows) | `schema.quality` library `rowCount` | **quality gate, on arrival (now)** |
| Schema conformity | `schema.properties` | **quality gate, on arrival (now)** |

Schema conformity and completeness run **in the landing gate today**. Timeliness and
freshness are **declared now** and **monitored at M4** (DataHub assertions + alerting), because
alerting needs a destination, and because enforcing content-freshness against the static
historical Olist snapshot would always fail. The contract is the one place they live; CI,
the gate, and DataHub all read from it.

### 2. Partitioning: source-aligned, daily
*(Superseded by ADR-0024: the partner's path is free-form under `landing-inbox/<source>/<dataset>/`;
the platform files accepted deliveries to `landing-archive/accepted/<source>/<dataset>/<partition>/`.)*
Landing objects were keyed `<source>/<dataset>/dt=YYYY-MM-DD/<file>` (hourly is a
config switch). Daily partitions align with Airflow data intervals (ADR-0006), enable
incremental processing and backfills, scope freshness/completeness per partition, and let
retention expire whole partitions.

### 3. Retention
Landing and state buckets keep **current** versions and expire **superseded/deleted** versions
after the retention window (365 days locally). *(Superseded by ADR-0024: accepted files are
object-locked per object for the contract's `retentionDays` class; quarantine expires at 90 days.)*

### 4. Shared datasets: location, ownership and consumption are separated
A landing dataset is consumed by many domains but must have **one** accountable owner. We
separate the three concerns that `<domain>`-in-the-path used to conflate:

- **Location is source-aligned, never consumer-aligned.** The path reflects where data comes
  from (`<source>/<dataset>`), so a shared file does not live under any consumer's name.
- **Ownership is singular and declared.** Exactly one owner per landing dataset — the ODCS
  `team`, mirrored by which domain registers it in its `sources.yml` and by `CODEOWNERS`.
  Cross-cutting reference data (e.g. geolocation) is owned by the platform-level `reference`
  domain.
- **Consumption is many, through the output port.** Other domains never read the landing file
  directly; they consume the owning domain's published data product. One owner, N consumers.

## Consequences
- The contract is the single source of truth for schema, quality **and** SLAs — no second place
  to keep "the KPIs".
- The landing path no longer implies ownership or consumption; re-assigning ownership is a
  contract/`CODEOWNERS` change, not a data move.
- Time-based SLA enforcement (timeliness, freshness) and event-driven triggering land in M4 with
  DataHub; until then they are declared and checked structurally.
