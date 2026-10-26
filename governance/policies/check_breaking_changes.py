#!/usr/bin/env python3
"""Breaking-change contract gate (proof P5).

For every **output-port** contract changed in this PR (``domains/*/contracts/*.odcs.yaml``, not
the landing ones), compare it against the base branch with ``datacontract breaking``. An
ERROR-severity change (a removed/renamed/narrowed field) **fails the gate** and names the
consuming teams that would be affected — pulled from the data-product definitions — so an
interface can never break silently.

Shipping a breaking change requires a major version bump + consumer approval, or a new major as a
versioned port ``<port>_v<N>`` beside the old one (ADR-0019 §3). Retiring a port (deleting its
contract) is breaking too: blocked while any data product still reads it, and it needs approval from
its declared consumers in ``governance/approvals/<port>-retire.yaml``.

Run: ``make check-breaking`` (or in CI with ``BASE_REF=origin/<target>``).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml
from dgp_governance import approvals, products
from dgp_governance.repo import load_yaml, root
from dgp_platform_contracts import odcs

BASE_REF = os.environ.get("BASE_REF", "origin/main")


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root(), capture_output=True, text=True)


def _resolve_base() -> str | None:
    for ref in (BASE_REF, "main"):
        if _git("rev-parse", "--verify", ref).returncode == 0:
            return ref
    return None


def _changed_output_port_contracts(base: str) -> list[Path]:
    """Output-port contracts changed vs base — the file itself, or a definition it links to (a
    changed concept changes every contract that uses it). Landing contracts live one level deeper
    (``contracts/landing/``) and are not output ports."""
    changed = set(_git("diff", "--name-only", base).stdout.splitlines())
    # New files not committed yet (a local run on a working tree); a PR has them in the diff.
    changed |= set(_git("ls-files", "--others", "--exclude-standard").stdout.splitlines())
    out = []
    for path in products.contract_files():
        reached = {path.resolve(), *odcs.linked_files(path)}
        rel = {p.relative_to(root()).as_posix() for p in reached if p.is_relative_to(root())}
        if rel & changed:
            out.append(path)
    return out


def _retired_output_ports(base: str) -> list[tuple[str, str]]:
    """(domain, port) of the output-port contracts deleted vs base (a version being retired)."""
    deleted = _git("diff", "--name-only", "--diff-filter=D", base, "--", "domains").stdout
    out = []
    for f in deleted.splitlines():
        parts = f.split("/")
        if len(parts) != 4 or parts[2] != "contracts" or not f.endswith(".odcs.yaml"):
            continue
        schema = odcs.output_port(odcs.load(root() / f, read=_at_base(base)))
        if schema is not None:
            out.append((parts[1], schema.get("name", parts[3].removesuffix(".odcs.yaml"))))
    return out


def _declared_consumers(base: str, domain: str, port: str) -> set[str]:
    """The consumers the retired port's definition declared, as of the base branch."""
    defn = yaml.safe_load(_git("show", f"{base}:domains/{domain}/data_products/{port}.yaml").stdout)
    return {c["team"] for c in ((defn or {}).get("consumers") or []) if c.get("team")}


def _retirement_blockers(base: str, domain: str, port: str) -> tuple[list[str], set[str]]:
    """(data products still reading it, declared consumers that have not approved)."""
    approved = approvals.approved_teams(port, approvals.RETIRE)
    return products.readers_of(domain, port), _declared_consumers(base, domain, port) - approved


def _at_base(base: str) -> odcs.Reader:
    """Read a repository file as it is on the base branch, so a base contract's links resolve to
    the definitions as they were then too (a changed concept is a changed contract)."""

    def read(path: Path) -> str:
        r = _git("show", f"{base}:{path.relative_to(root()).as_posix()}")
        if r.returncode != 0:
            raise FileNotFoundError(path)
        return r.stdout

    return read


def _resolved_file(doc: dict) -> str:
    """A resolved contract in a temporary file, for ``datacontract breaking``."""
    fd, tmp = tempfile.mkstemp(suffix=".odcs.yaml")
    os.close(fd)
    Path(tmp).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return tmp


def _base_file(base: str, path: Path) -> str | None:
    """The contract as it is on the base branch, resolved, in a temporary file (None if new)."""
    try:
        return _resolved_file(odcs.load(path, read=_at_base(base)))
    except FileNotFoundError:
        return None


def _breaking_report(old_file: str, new_file: Path) -> str | None:
    """The ``datacontract breaking`` report if it finds ERROR-severity changes, else None. (The
    command always exits 0, so this reads its ``[ N Error ]`` summary.)"""
    dc = shutil.which("datacontract") or "datacontract"
    # Both sides are compared resolved, so an inherited change counts. The temporary copies carry
    # their definitions already, and their relative links would not resolve from /tmp anyway.
    new = _resolved_file(odcs.load(Path(new_file)))
    r = subprocess.run(
        [dc, "breaking", "--no-inline-references", old_file, new], capture_output=True, text=True
    )
    out = r.stdout + r.stderr
    m = re.search(r"\[\s*(\d+)\s+Error", out)
    return out if (m and int(m.group(1)) > 0) else None


def _version(file: str | Path) -> str:
    return str(load_yaml(Path(file)).get("version", "0"))


def _major(version: str) -> int:
    head = version.split(".")[0]
    return int(head) if head.isdigit() else 0


def _port_name(path: Path) -> str:
    return path.name.removesuffix(".odcs.yaml")


def _impact(base: str) -> list[dict]:
    """Breaking changes that still need approval: [{port, version, teams}] — the approval
    workflow reads this to know whose review to request (ADR-0019)."""
    out = []
    for path in _changed_output_port_contracts(base):
        old = _base_file(base, path)
        if old is None or _breaking_report(old, path) is None:
            continue
        port, new_major = _port_name(path), _major(_version(path))
        teams = sorted(products.affected_teams(port) - approvals.approved_teams(port, new_major))
        out.append({"port": port, "version": new_major, "teams": teams})
    for domain, port in _retired_output_ports(base):
        _, missing = _retirement_blockers(base, domain, port)
        out.append({"port": port, "version": approvals.RETIRE, "teams": sorted(missing)})
    return out


def _check_retirement(base: str, domain: str, port: str) -> bool:
    """Report on one retired port version; True if its retirement is blocked."""
    readers, missing = _retirement_blockers(base, domain, port)
    print(f"\nRETIRING output port '{domain}.{port}' (its contract is deleted vs {base}):")
    if readers:
        print(
            f"  BLOCKED: still read by {', '.join(readers)} — migrate them to the successor "
            "version first."
        )
        return True
    if missing:
        print(
            f"  BLOCKED: retirement needs approval from: {', '.join(sorted(missing))} "
            f"({approvals.path(port, approvals.RETIRE).relative_to(root())})."
        )
        return True
    print("  APPROVED: no data product reads it and its declared consumers signed off.")
    return False


def _check_change(base: str, path: Path) -> bool:
    """Report on one changed output-port contract; True if the change is blocked."""
    product = _port_name(path)
    old = _base_file(base, path)
    if old is None:
        print(f"  {product}: new output-port contract (no prior version) — OK.")
        return False
    breaking = _breaking_report(old, path)
    if breaking is None:
        print(f"  {product}: contract changed, no breaking changes — OK.")
        return False

    # A breaking change needs a MAJOR version bump, and approval from every affected consumer.
    consumers = products.affected_teams(product)
    old_major, new_major = _major(_version(old)), _major(_version(path))
    print(f"\nBREAKING CHANGE in output port '{product}' (vs {base}):\n")
    print(breaking)
    print(f"  Affected consumers: {', '.join(sorted(consumers)) or '(none declared)'}")

    if new_major <= old_major:
        print(
            f"  BLOCKED: a breaking change needs a MAJOR version bump (contract is "
            f"{_version(old)} -> {_version(path)}), then consumer approval. Migrate the "
            f"consumers with the change; if one can't cut over at once, ship the new major "
            f"as a versioned port '{product}_v{old_major + 1}' instead "
            f"(governance/standards/breaking-changes.md)."
        )
        return True

    missing = consumers - approvals.approved_teams(product, new_major)
    if missing:
        print(
            f"  BLOCKED: v{new_major} still needs approval from: {', '.join(sorted(missing))}. "
            f"They approve the PR and the workflow records it in "
            f"{approvals.path(product, new_major).relative_to(root())} (or commit it by hand)."
        )
        return True

    print(
        f"  APPROVED: v{new_major} breaking change signed off by all affected consumers "
        f"({', '.join(sorted(consumers)) or 'none required'}); migrate them with the change "
        f"(version the port only if a consumer can't cut over at once)."
    )
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Breaking-change contract gate (proof P5).")
    parser.add_argument(
        "--impact-json",
        action="store_true",
        help="print breaking changes still needing approval as JSON (for the approval workflow)",
    )
    args = parser.parse_args()

    base = _resolve_base()
    if not base:
        if args.impact_json:
            print("[]")
            return 0
        print(f"Breaking-change gate: base ref '{BASE_REF}' not found — skipping (not a PR).")
        return 0

    if args.impact_json:
        print(json.dumps(_impact(base)))
        return 0

    contracts = _changed_output_port_contracts(base)
    retired = _retired_output_ports(base)
    if not contracts and not retired:
        print(f"Breaking-change gate OK: no output-port contract changed vs {base}.")
        return 0

    blocked = [_check_retirement(base, domain, port) for domain, port in retired]
    blocked += [_check_change(base, path) for path in contracts]
    failed = any(blocked)
    if failed:
        print("\nBreaking-change gate FAILED (proof P5): an output-port interface would break.")
        return 1
    print(f"Breaking-change gate OK: output-port contract(s) changed vs {base}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
