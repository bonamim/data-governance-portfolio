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
    # New contracts not yet committed (a local run on a working tree) — a PR has them in the diff.
    changed += _git("ls-files", "--others", "--exclude-standard", "domains").stdout.splitlines()
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


def _retired_output_ports(base: str) -> list[tuple[str, str]]:
    """(domain, port) of output-port contracts deleted vs base (a version being retired)."""
    deleted = _git("diff", "--name-only", "--diff-filter=D", base, "--", "domains").stdout
    out = []
    for f in deleted.splitlines():
        parts = f.split("/")
        if len(parts) != 4 or parts[2] != "contracts" or not f.endswith(".odcs.yaml"):
            continue
        doc = yaml.safe_load(_git("show", f"{base}:{f}").stdout) or {}
        schema = (doc.get("schema") or [{}])[0]
        if "output_port" in (schema.get("tags") or []):
            out.append((parts[1], schema.get("name", parts[3][: -len(".odcs.yaml")])))
    return out


def _readers(domain: str, port: str) -> list[str]:
    """Data products that still consume ``<domain>.<port>`` as an input (they would break)."""
    out = []
    for dp in sorted(REPO_ROOT.glob("domains/*/data_products/*.yaml")):
        defn = yaml.safe_load(dp.read_text(encoding="utf-8")) or {}
        if f"{domain}.{port}" in (defn.get("inputs") or []):
            out.append(f"{dp.parts[-3]}.{defn.get('name', dp.stem)}")
    return out


def _declared_consumers(base: str, domain: str, port: str) -> set[str]:
    """The consumers the retired port's definition declared (as of the base branch)."""
    defn = yaml.safe_load(_git("show", f"{base}:domains/{domain}/data_products/{port}.yaml").stdout)
    return {c["team"] for c in ((defn or {}).get("consumers") or []) if c.get("team")}


def _retirement_blockers(base: str, domain: str, port: str) -> tuple[list[str], set[str]]:
    """(data products still reading it, declared consumers that have not approved)."""
    approved = _approved_teams(port, "retire")
    return _readers(domain, port), _declared_consumers(base, domain, port) - approved


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


def _version(file: str | Path) -> str:
    defn = yaml.safe_load(Path(file).read_text(encoding="utf-8")) or {}
    return str(defn.get("version", "0"))


def _major(version: str) -> int:
    head = version.split(".")[0]
    return int(head) if head.isdigit() else 0


def _approved_teams(product: str, major: int | str) -> set[str]:
    """Teams recorded as approving this port's v<major> breaking change, or its retirement
    (``major="retire"``), in governance/approvals/. In production the approval workflow writes this
    file when consumers approve the PR; it can also be committed by hand. The file is the
    authority — GitHub is only the automation."""
    suffix = "retire" if major == "retire" else f"v{major}"
    f = REPO_ROOT / "governance" / "approvals" / f"{product}-{suffix}.yaml"
    if not f.exists():
        return set()
    defn = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    return {a["team"] for a in (defn.get("approved_by") or []) if a.get("team")}


def _impact(base: str) -> list[dict]:
    """Breaking changes that still need approval: [{port, version, teams}].

    Consumed by the approval workflow to know whose review to request (ADR-0019)."""
    out = []
    for path in _changed_output_port_contracts(base):
        old = _base_file(base, path)
        if old is None or _breaking_report(old, path) is None:
            continue
        product = path.name[: -len(".odcs.yaml")]
        new_major = _major(_version(path))
        teams = sorted(_consumers_of(product) - _approved_teams(product, new_major))
        out.append({"port": product, "version": new_major, "teams": teams})
    for domain, port in _retired_output_ports(base):
        _, missing = _retirement_blockers(base, domain, port)
        out.append({"port": port, "version": "retire", "teams": sorted(missing)})
    return out


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

    failed = False
    for domain, port in retired:
        readers, missing = _retirement_blockers(base, domain, port)
        print(f"\nRETIRING output port '{domain}.{port}' (its contract is deleted vs {base}):")
        if readers:
            failed = True
            print(f"  BLOCKED: still read by {', '.join(readers)} — migrate them to the "
                  "successor version first.")  # fmt: skip
        elif missing:
            failed = True
            print(f"  BLOCKED: retirement needs approval from: {', '.join(sorted(missing))} "
                  f"(governance/approvals/{port}-retire.yaml).")  # fmt: skip
        else:
            print("  APPROVED: no data product reads it and its declared consumers signed off.")
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

        # Breaking change — allowed only with a MAJOR version bump + approval from every consumer.
        consumers = _consumers_of(product)
        old_major, new_major = _major(_version(old)), _major(_version(path))
        print(f"\nBREAKING CHANGE in output port '{product}' (vs {base}):\n")
        print(report)
        print(f"  Affected consumers: {', '.join(sorted(consumers)) or '(none declared)'}")

        if new_major <= old_major:
            failed = True
            print(
                f"  BLOCKED: a breaking change needs a MAJOR version bump (contract is "
                f"{_version(old)} -> {_version(path)}), then consumer approval. Migrate the "
                f"consumers with the change; keep v{old_major} as a dbt model version only if a "
                f"consumer can't cut over atomically (ADR-0019)."
            )
            continue

        missing = consumers - _approved_teams(product, new_major)
        if missing:
            failed = True
            print(
                f"  BLOCKED: v{new_major} still needs approval from: {', '.join(sorted(missing))}. "
                f"They approve the PR and the workflow records it in "
                f"governance/approvals/{product}-v{new_major}.yaml (or commit it by hand)."
            )
            continue

        print(
            f"  APPROVED: v{new_major} breaking change signed off by all affected consumers "
            f"({', '.join(sorted(consumers)) or 'none required'}); migrate them with the change "
            f"(version the port only if a consumer can't cut over atomically)."
        )

    if failed:
        print("\nBreaking-change gate FAILED (proof P5): an output-port interface would break.")
        return 1
    print(f"Breaking-change gate OK: output-port contract(s) changed vs {base}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
