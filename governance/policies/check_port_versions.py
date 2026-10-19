#!/usr/bin/env python3
"""Versioned output ports (ADR-0019 §3): a new major lives beside the old until a sunset date.

When a breaking change must ship but a consumer can't cut over atomically (an external team, a BI
dashboard, ad-hoc SQL), the port gets a new major as its own port, ``<port>_v<N>``: its own ODCS
contract (major N), dbt model and data product definition carrying ``version_of: <port>``. The old
version keeps its contract, model and relation untouched — nobody is broken on release — and is
marked ``status: deprecated`` with a ``sunsetDate`` custom property: the end of the migration
window.

This policy enforces the lifecycle:

  - ``<port>_v<N>`` declares ``version_of`` a port of the same owner, and its contract is major N
  - a port with a successor is deprecated, with a sunset date (a release starts a window that ends)
  - a deprecated port has a successor and a sunset date
  - before the sunset, consumers still reading the deprecated version are listed (a notice)
  - once the sunset has passed, CI fails: the version must be retired, after its consumers move
    (retiring it is then a deletion the breaking-change gate checks)

``DGP_TODAY=YYYY-MM-DD`` overrides today (tests, proofs). Run: ``make check-port-versions``.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
VERSIONED = re.compile(r"^(?P<base>.+)_v(?P<major>\d+)$")


@dataclass
class Port:
    name: str
    domain: str
    contract: Path
    major: int
    status: str
    sunset: date | None
    version_of: str | None


def _today() -> date:
    return (
        date.fromisoformat(os.environ["DGP_TODAY"]) if os.environ.get("DGP_TODAY") else date.today()
    )


def ports(root: Path = REPO_ROOT) -> dict[str, Port]:
    defs = {}
    for p in root.glob("domains/*/data_products/*.yaml"):
        defs[p.stem] = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    out = {}
    for c in root.glob("domains/*/contracts/*.odcs.yaml"):
        doc = yaml.safe_load(c.read_text(encoding="utf-8")) or {}
        schema = (doc.get("schema") or [{}])[0]
        if "output_port" not in (schema.get("tags") or []):
            continue
        name = schema.get("name", c.stem)
        props = {p.get("property"): p.get("value") for p in doc.get("customProperties") or []}
        sunset = props.get("sunsetDate")
        out[name] = Port(
            name=name,
            domain=c.relative_to(root).parts[1],
            contract=c,
            major=int(str(doc.get("version", "0")).split(".")[0] or 0),
            status=str(doc.get("status", "active")),
            sunset=date.fromisoformat(str(sunset)) if sunset else None,
            version_of=(defs.get(name) or {}).get("version_of"),
        )
    return out


def consumers_reading(port: Port, root: Path = REPO_ROOT) -> list[str]:
    """Data products that consume this exact port version (``inputs: [<domain>.<port>]``)."""
    out = []
    for p in sorted(root.glob("domains/*/data_products/*.yaml")):
        defn = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if f"{port.domain}.{port.name}" in (defn.get("inputs") or []):
            out.append(f"{p.parts[-3]}.{defn.get('name', p.stem)}")
    return out


def violations(root: Path = REPO_ROOT, today: date | None = None) -> tuple[list[str], list[str]]:
    """(violations, notices)."""
    today = today or _today()
    all_ports = ports(root)
    errs: list[str] = []
    notes: list[str] = []
    successors = {p.version_of: p for p in all_ports.values() if p.version_of}

    for p in all_ports.values():
        where = p.contract.relative_to(root)
        m = VERSIONED.match(p.name)
        if p.version_of:
            base = all_ports.get(p.version_of)
            if not m or m["base"] != p.version_of:
                errs.append(
                    f"{p.name}: a version of '{p.version_of}' is named '{p.version_of}_v<major>'"
                )
            elif int(m["major"]) != p.major:
                errs.append(
                    f"{where}: '{p.name}' must carry major {m['major']} (contract is {p.major}.x)"
                )
            if base is not None and base.domain != p.domain:
                errs.append(f"{p.name}: a version stays with its owner '{base.domain}'")
        elif m and m["base"] in all_ports:
            errs.append(
                f"{p.name}: looks like a version of '{m['base']}' — declare "
                f"`version_of: {m['base']}` in its data product definition"
            )

        if p.name in successors and p.status != "deprecated":
            nxt = successors[p.name].name
            errs.append(
                f"{where}: '{p.name}' has a successor ({nxt}) — mark it "
                "`status: deprecated` with a `sunsetDate` (the migration window)"
            )
        if p.status == "deprecated":
            if p.sunset is None:
                errs.append(f"{where}: deprecated without a `sunsetDate` custom property")
            if p.name not in successors:
                errs.append(f"{where}: deprecated with no successor (`<port>_v<N>`, version_of)")
            readers = consumers_reading(p, root)
            if p.sunset is not None and p.sunset <= today:
                errs.append(
                    f"{p.name}: sunset {p.sunset} has passed — retire it"
                    + (f" after migrating {', '.join(readers)}" if readers else "")
                )
            elif p.sunset is not None:
                left = (p.sunset - today).days
                notes.append(
                    f"{p.name} is deprecated, sunset {p.sunset} ({left} days): "
                    + (f"still read by {', '.join(readers)}" if readers else "no dbt consumer left")
                    + f"; successor {successors[p.name].name}" * (p.name in successors)
                )
    return errs, notes


def main() -> int:
    errs, notes = violations()
    for n in notes:
        print(f"  notice: {n}")
    if errs:
        print("Port versioning violations (ADR-0019):\n")
        for e in errs:
            print(f"  - {e}")
        return 1
    versioned = sum(1 for p in ports().values() if p.version_of or p.status == "deprecated")
    print(f"Port versions OK: {len(ports())} port(s), {versioned} in a versioned lifecycle.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
