#!/usr/bin/env python3
"""Federated governance policy: every container image is pinned (production-readiness X8).

A tag (even ``3.3.2``) is mutable; a digest is not. Rules (a violation fails CI):
  1. every ``image:`` in a compose file is pinned by ``@sha256:`` — unless the service also has a
     ``build:`` (a locally built image, whose base is checked by rule 2);
  2. every ``FROM`` in a Dockerfile is pinned by ``@sha256:``;
  3. nothing uses ``latest``;
  4. every GitHub Action (``uses:``) is pinned to a full 40-character commit SHA — a tag can be
     moved to malicious code; a SHA cannot (local ``./`` actions excepted).

Stdlib only, line-based (compose ``image:`` keys and Dockerfile ``FROM`` lines are flat).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKIP_PARTS = {".venv", "node_modules", ".git", "target", "dbt_packages"}
IMAGE = re.compile(r"^(\s*)image:\s*['\"]?([^'\"\s#]+)")
FROM = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)", re.IGNORECASE)
USES = re.compile(r"^\s*(?:-\s*)?uses:\s*['\"]?([^'\"\s#]+)")
SHA = re.compile(r"@[0-9a-f]{40}$")


def _files(pattern: str) -> list[Path]:
    return [p for p in REPO_ROOT.rglob(pattern) if not SKIP_PARTS & set(p.parts)]


def compose_violations(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    for n, line in enumerate(lines):
        m = IMAGE.match(line)
        if not m:
            continue
        indent, image = len(m.group(1)), m.group(2)
        # A sibling `build:` key at the same indent within this service block = locally built.
        block = []
        for other in lines[n + 1 :] + []:
            if other.strip() and len(other) - len(other.lstrip()) < indent:
                break
            block.append(other)
        for other in reversed(lines[:n]):
            if other.strip() and len(other) - len(other.lstrip()) < indent:
                break
            block.append(other)
        built = any(re.match(rf"^\s{{{indent}}}build:", b) for b in block)
        if image.endswith(":latest") or (not built and "@sha256:" not in image):
            out.append(
                f"{path.relative_to(REPO_ROOT)}:{n + 1}: image not pinned by digest: {image}"
            )
    return out


def dockerfile_violations(path: Path) -> list[str]:
    out: list[str] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        m = FROM.match(line)
        if m and "@sha256:" not in m.group(1):
            out.append(
                f"{path.relative_to(REPO_ROOT)}:{n}: base image not pinned by digest: {m.group(1)}"
            )
    return out


def workflow_violations(path: Path) -> list[str]:
    out: list[str] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        m = USES.match(line)
        if m and not m.group(1).startswith("./") and not SHA.search(m.group(1)):
            out.append(
                f"{path.relative_to(REPO_ROOT)}:{n}: action not pinned to a commit SHA: "
                f"{m.group(1)}"
            )
    return out


def main() -> int:
    composes = _files("docker-compose*.yml")
    dockerfiles = _files("Dockerfile*")
    violations = [v for p in composes for v in compose_violations(p)]
    violations += [v for p in dockerfiles for v in dockerfile_violations(p)]
    workflows = sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
    violations += [v for p in workflows for v in workflow_violations(p)]
    if violations:
        print("Unpinned images (X8):")
        for v in violations:
            print(f"  - {v}")
        return 1
    print(
        f"All images pinned by digest ({len(composes)} compose files, "
        f"{len(dockerfiles)} Dockerfiles); all actions pinned by SHA ({len(workflows)} workflows)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
