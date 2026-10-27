# ADR-0017: Local runtime topology: one VM per environment, capability compose projects

**Status:** Accepted
**Extends** [ADR-0004](0004-colima-local-runtime.md) (Colima local runtime).

## Context
As the stack grew (object store, two Postgres, Airflow, dbt, and now DataHub), it was unclear
how to split it across Colima VMs. An early attempt put the whole platform in one VM named
`governance` and spun up a **second VM just for DataHub**, which conflated two different axes
(a *theme* name vs a *component* name) and implied that "one VM per component" is the isolation
model. It is not, and the confusion is worth settling before M4 adds DataHub.

## Decision

### 1. The container is the deployable, and the environment is the isolation boundary
In production nothing is isolated "per VM". Containers are scheduled by an orchestrator
(Kubernetes / ECS), and isolation comes from **environments** (dev / staging / prod / DR as separate
clusters or namespaces), **per-service resource limits**, and **network policies**. We mirror
that, not a VM-per-service layout.

### 2. One Colima VM = one environment, and capabilities are compose projects
A Colima VM is treated as **one environment**: a Docker host with a CPU/RAM budget, the local
stand-in for *one cluster*. Capabilities are isolated at the **docker-compose project** layer on a
**shared network**, not by separate VMs:

| Compose project | Contains |
|---|---|
| `dgp-platform` | object store (MinIO) + the two Postgres + Airflow + dbt runtime |
| `dgp-catalog` | DataHub (and its Elasticsearch / Kafka) |

Separate compose projects on a shared network give real logical isolation (independent lifecycle,
per-service resource limits) **and** trivial connectivity for lineage/ingestion, the faithful
local mirror of "separate workloads in one cluster". A second *VM* for DataHub would only add
OS/Docker overhead and cross-VM networking while mirroring prod no better than a compose project
does.

### 3. Multiple VMs are for environments, not components
The legitimate reason to run more than one VM in this project is the **prod + DR** requirement:
a `dgp-primary` and a `dgp-dr` environment (future, with the DR work). DataHub is just one
containerised capability *within* an environment.

### 4. Naming convention
- **VM / Colima profile** → the **environment** (e.g. `dgp-local`, later `dgp-primary` / `dgp-dr`).
- **Compose project** → the **capability** (`dgp-platform`, `dgp-catalog`).

The current VM keeps its `governance` profile name for now. Renaming a Colima profile means
rebuilding the VM and reloading data, which is not worth a cosmetic change mid-project. The rename
to `dgp-local` lands at the next natural rebuild (most likely when the DR environment VM is created
and the convention matters).

## Consequences
- DataHub is added as the `dgp-catalog` compose project in the existing VM (resized for its
  footprint), not as a separate VM, and the abandoned `datahub` VM is removed.
- Each compose service maps, in production, to its own managed service or Kubernetes workload with
  resource requests/limits and network policy (production-readiness.md).
- The environment axis (primary/DR) is where multi-VM returns, aligning with the disaster-recovery
  requirement.
