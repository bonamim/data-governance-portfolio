#!/usr/bin/env python3
"""Slim CI (ADR-0015 §3): build only what a change touched, and defer the rest to what is published.

For each domain (dependency order) the change is compared with a baseline manifest:

  state        the manifest the last successful publish saved to platform-state (default)
  git:<ref>    the projects parsed at a git ref. Needs no warehouse, so CI can run it

  dbt ls    --select state:modified+ --state <baseline>                  what the change touches
  dbt build --select state:modified+ --defer --state <baseline> --target ci
                                                    unless --plan-only: the changed models and
                                                    their descendants are built and tested in the
                                                    <domain>_ci sandbox, and every unchanged ref is
                                                    read from the published relations

Run: ``make slim-ci`` (local, against the warehouse) or, in GitHub Actions,
``slim_ci.py --baseline git:origin/main --plan-only``.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from dgp_governance.repo import root
from dgp_platform_orchestration import dbt_state, wap

REPO_ROOT = root()


def _dbt(root: Path, domain: str, *args: str, capture: bool = False) -> subprocess.CompletedProcess:
    exe = shutil.which("dbt") or "dbt"
    cmd = [exe, *args, "--profiles-dir", str(REPO_ROOT / "platform" / "core" / "transform")]
    return subprocess.run(
        cmd, cwd=wap.project_dir(root, domain), capture_output=capture, text=True, check=False
    )


def _parse(root: Path, domains: list[str]) -> None:
    """deps + parse in dependency order (dbt-loom reads the upstream projects' manifests)."""
    for d in domains:
        for step in (["deps"], ["parse"]):
            r = _dbt(root, d, *step, "--quiet", capture=True)
            if r.returncode:
                sys.exit(f"dbt {step[0]} failed for {d} at {root}:\n{r.stdout[-2000:]}")


def _baseline(spec: str, domains: list[str], tmp: Path) -> dict[str, Path | None]:
    """domain -> directory holding the baseline manifest.json (None: no baseline yet)."""
    if spec == "state":
        return {d: (p.parent if (p := dbt_state.fetch(d, tmp / d)) else None) for d in domains}
    if not spec.startswith("git:"):
        sys.exit(f"unknown baseline {spec!r} (state | git:<ref>)")
    tree = tmp / "baseline"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(tree), spec[4:]],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )
    _parse(tree, wap.DOMAINS)  # all of them: dbt-loom needs the upstream manifests
    return {d: wap.project_dir(tree, d) / "target" for d in domains}


def main() -> int:
    parser = argparse.ArgumentParser(description="Slim CI: build only what a change touched.")
    parser.add_argument("--domain", help="one domain (default: all, in dependency order)")
    parser.add_argument("--baseline", default="state", help="state (default) | git:<ref>")
    parser.add_argument("--plan-only", action="store_true", help="list, don't build")
    args = parser.parse_args()
    domains = [args.domain] if args.domain else wap.DOMAINS

    _parse(REPO_ROOT, wap.DOMAINS)  # the change's own manifests, upstream first
    exit_code = 0
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        try:
            baselines = _baseline(args.baseline, domains, tmp)
            for d in domains:
                state = baselines[d]
                if state is None:
                    print(f"{d}: no published baseline yet. A full `make publish` comes first")
                    continue
                sel = ["--select", "state:modified+", "--state", str(state)]
                ls = _dbt(
                    REPO_ROOT,
                    d,
                    "ls",
                    *sel,
                    "--resource-type",
                    "model",
                    "--output",
                    "name",
                    "--quiet",
                    capture=True,
                )
                if ls.returncode:
                    print(ls.stdout[-2000:])
                    return 1
                own = {
                    n["name"]
                    for n in wap.load_manifest(REPO_ROOT, d)["nodes"].values()
                    if n["package_name"] == wap.project_name(d)
                }
                changed = sorted(n for n in ls.stdout.split() if n in own)
                print(f"{d}: {len(changed)} model(s) to build: {', '.join(changed) or '(none)'}")
                if args.plan_only or not changed:
                    continue
                if _dbt(REPO_ROOT, d, "run-operation", "ci_reset", "--target", "ci").returncode:
                    return 1
                build = _dbt(
                    REPO_ROOT,
                    d,
                    "build",
                    *sel,
                    "--defer",
                    "--target",
                    "ci",
                    "--target-path",
                    "target/ci",
                )
                if build.returncode:
                    exit_code = 1
        finally:
            if args.baseline.startswith("git:"):
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(tmp / "baseline")],
                    cwd=REPO_ROOT,
                    check=False,
                    capture_output=True,
                )
    return exit_code


if __name__ == "__main__":
    os.environ.setdefault("DBT_SEND_ANONYMOUS_USAGE_STATS", "false")
    sys.exit(main())
