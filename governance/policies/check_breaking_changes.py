#!/usr/bin/env python3
"""Breaking-change contract gate (proof P5).

For every **output-port** contract changed in this PR (``domains/*/contracts/*.odcs.yaml``, not
the landing ones), compare it against the base branch with ``datacontract breaking``. An
ERROR-severity change (a removed/renamed/narrowed field) **fails the gate** and names the
consuming teams that would be affected — pulled from the data-product definitions — so an
interface can never break silently.

Shipping a breaking change requires a major version bump + consumer approval, released as a new
dbt model version with the old one kept (the M5 escape hatch).

Run: ``make check-breaking`` (or in CI with ``BASE_REF=origin/<target>``).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_REF = os.environ.get("BASE_REF", "origin/main")


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True)


def _resolve_base() -> str | None:
    for ref in (BASE_REF, "main"):
        if _git("rev-parse", "--verify", ref).returncode == 0:
            return ref
    return None


def _changed_output_port_contracts(base: str) -> list[Path]:
    """Output-port contracts changed vs base — domains/<d>/contracts/<port>.odcs.yaml (depth 4;
    landing contracts live one level deeper under contracts/landing/ and are excluded)."""
    changed = _git("diff", "--name-only", base, "--", "domains").stdout.splitlines()
    out = []
    for f in changed:
        parts = f.split("/")
        if (
            len(parts) == 4
            and parts[0] == "domains"
            and parts[2] == "contracts"
            and f.endswith(".odcs.yaml")
            and (REPO_ROOT / f).exists()
        ):
            out.append(REPO_ROOT / f)
    return out


def _base_file(base: str, path: Path) -> str | None:
    """The contract as it is on the base branch, written to a temp file (None if new)."""
    rel = path.relative_to(REPO_ROOT).as_posix()
    r = _git("show", f"{base}:{rel}")
    if r.returncode != 0:
        return None
    fd, tmp = tempfile.mkstemp(suffix=".odcs.yaml")
    os.close(fd)
    Path(tmp).write_text(r.stdout, encoding="utf-8")
    return tmp


def _consumers_of(product: str) -> set[str]:
    """Teams affected if this output port breaks: its declared consumers, plus any domain whose
    data product consumes it as a cross-domain input."""
    teams: set[str] = set()
    for dp in REPO_ROOT.glob("domains/*/data_products/*.yaml"):
        defn = yaml.safe_load(dp.read_text(encoding="utf-8")) or {}
        owner = (defn.get("owner") or {}).get("team")
        if defn.get("name") == product:
            for c in defn.get("consumers") or []:
                if c.get("team"):
                    teams.add(c["team"])
        for ref in defn.get("inputs") or []:
            if str(ref).split(".")[-1] == product and owner:
                teams.add(owner)
    return teams


def _breaking_report(old_file: str, new_file: Path) -> str | None:
    """datacontract breaking report if there are ERROR-severity changes, else None.
    (datacontract breaking always exits 0, so parse the '[ N Error ]' summary.)"""
    dc = shutil.which("datacontract") or "datacontract"
    r = subprocess.run([dc, "breaking", old_file, str(new_file)], capture_output=True, text=True)
    out = r.stdout + r.stderr
    m = re.search(r"\[\s*(\d+)\s+Error", out)
    return out if (m and int(m.group(1)) > 0) else None


def main() -> int:
    base = _resolve_base()
    if not base:
        print(f"Breaking-change gate: base ref '{BASE_REF}' not found — skipping (not a PR).")
        return 0

    contracts = _changed_output_port_contracts(base)
    if not contracts:
        print(f"Breaking-change gate OK: no output-port contract changed vs {base}.")
        return 0

    failed = False
    for path in contracts:
        product = path.name[: -len(".odcs.yaml")]
        old = _base_file(base, path)
        if old is None:
            print(f"  {product}: new output-port contract (no prior version) — OK.")
            continue
        report = _breaking_report(old, path)
        if report is None:
            print(f"  {product}: contract changed, no breaking changes — OK.")
            continue
        failed = True
        consumers = _consumers_of(product) or {"(no declared consumers)"}
        print(f"\nBREAKING CHANGE in output port '{product}' (vs {base}):\n")
        print(report)
        print(f"  Affected consumers: {', '.join(sorted(consumers))}")
        print(
            "  To ship this: bump the contract major version and get consumer approval, "
            "released as a new dbt model version with the old kept (ADR-0015 / M5)."
        )

    if failed:
        print("\nBreaking-change gate FAILED (proof P5): an output-port interface would break.")
        return 1
    print(f"Breaking-change gate OK: output-port contract(s) changed vs {base}, none breaking.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
