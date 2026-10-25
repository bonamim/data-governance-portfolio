# ADR-0007: MinIO container image source

**Status:** Accepted

## Context
The landing zone and platform-state store use **MinIO** (S3-compatible, AGPL). During
2025 MinIO's community distribution changed: new community images are no longer published
to Docker Hub (`docker.io/minio/minio` was removed) and the `quay.io/minio/minio`
repository now requires authentication for anonymous pulls. Separately, Bitnami moved its
catalogue to "Bitnami Secure Images" in August 2025 and froze the free Docker Hub tags,
relocating the last free builds to the `bitnamilegacy/*` namespace.

A portfolio must be reproducible by anyone with a clean Docker install, so the local image
has to be pullable without credentials or a commercial subscription.

## Decision
For the **local** stack, pin **`bitnamilegacy/minio`** and **`bitnamilegacy/minio-client`**
by digest. These are the genuine MinIO server and `mc` client, just Bitnami's last freely
pullable build. Pinning by digest makes the build immutable despite the `legacy` tag being
frozen.

For **production**, use an image your organisation controls: MinIO's official image via an
authenticated `quay.io` pull (or an AIStor subscription), or a mirror in your own registry.
Nothing in the platform depends on Bitnami specifics — the server is MinIO and the client is
`mc`, so only the `image:` lines in `platform/core/infra/docker-compose.yml` change.

## Consequences
- Reproducible local setup with no login and no paid subscription.
- The frozen `legacy` image will not receive security patches; this is acceptable for a
  local demo and is called out in [production-readiness.md](../production-readiness.md) X8
  (pin versions) and X7 (encryption at rest) — production uses a maintained, controlled image.
- This is itself a governance lesson the portfolio demonstrates: **supply-chain provenance of
  infrastructure images is part of data governance**, not separate from it.
