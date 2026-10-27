# ADR-0004: Colima as the local container runtime

**Status:** Accepted

## Context
The local stack runs in containers. Docker Desktop is proprietary and requires a paid
subscription in larger organisations. The project should be fully open source.

## Decision
Use **[Colima](https://github.com/abiosoft/colima)** (MIT) on macOS/Linux, with the
open-source Docker CLI and Docker Compose plugin (Apache 2.0).

## Consequences
- Compose files are standard. They also run on Docker Engine on Linux, Podman Compose,
  or Docker Desktop, so contributors are not locked in.
- The Colima VM must be sized for the full stack (DataHub alone needs several GB).
  See the README for the recommended profile.
- Production does not use Colima or Compose: the same images are deployed to
  Kubernetes or managed services (see production-readiness.md).
