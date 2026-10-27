#!/usr/bin/env python3
"""Versioned output ports (ADR-0019 §3): a new major lives beside the old until a sunset date.

When a breaking change must ship but a consumer can't cut over at once (an external team, a BI
dashboard, ad-hoc SQL), the port gets its new major as a port of its own, ``<port>_v<N>``: its own
ODCS contract (major N), dbt model and data product definition with ``version_of: <port>``. The
old version keeps its contract, model and relation untouched, so nobody breaks on release, and is
marked ``status: deprecated`` with a ``sunsetDate`` custom property: the end of the migration
window.

The lifecycle this enforces:

- ``<port>_v<N>`` declares ``version_of`` a port of the same owner, and its contract is major N.
- a port with a successor is deprecated, with a sunset date (a release starts a window that ends).
- a deprecated port has a successor and a sunset date.
- before the sunset, the consumers still reading the old version are listed (a notice).
- once the sunset has passed, CI fails: the version must be retired, after its consumers move
  (and retiring it is a deletion the breaking-change gate checks).

``DGP_TODAY=YYYY-MM-DD`` overrides today (tests, proofs). Run: ``make check-port-versions``.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import date

from _checks import report
from dgp_governance import checks, products
from dgp_governance.repo import relative

VERSIONED = re.compile(r"^(?P<base>.+)_v(?P<major>\d+)$")


def _today() -> date:
    configured = os.environ.get("DGP_TODAY")
    return date.fromisoformat(configured) if configured else date.today()


def _naming_violations(
    port: products.OutputPort, version_of: str | None, ports: dict[str, products.OutputPort]
) -> list[str]:
    """A version is named ``<port>_v<major>``, carries that major, and stays with its owner."""
    m = VERSIONED.match(port.name)
    if version_of is None:
        if m and m["base"] in ports:
            return [
                f"{port.name}: looks like a version of '{m['base']}'. Declare "
                f"`version_of: {m['base']}` in its data product definition"
            ]
        return []
    errs = []
    if not m or m["base"] != version_of:
        errs.append(f"{port.name}: a version of '{version_of}' is named '{version_of}_v<major>'")
    elif int(m["major"]) != port.major:
        errs.append(
            f"{relative(port.contract)}: '{port.name}' must carry major {m['major']} "
            f"(contract is {port.major}.x)"
        )
    base = ports.get(version_of)
    if base is not None and base.domain != port.domain:
        errs.append(f"{port.name}: a version stays with its owner '{base.domain}'")
    return errs


def violations(today: date | None = None) -> tuple[list[str], list[str]]:
    """(violations, notices) for every output port's version lifecycle."""
    today = today or _today()
    ports = products.output_ports()
    version_of = {p.name: p.version_of for p in products.data_products()}
    successors = {version_of[n]: ports[n] for n in ports if version_of.get(n)}
    errs: list[str] = []
    notes: list[str] = []

    for port in ports.values():
        where = relative(port.contract)
        errs += _naming_violations(port, version_of.get(port.name), ports)
        successor = successors.get(port.name)
        if successor and port.status != "deprecated":
            errs.append(
                f"{where}: '{port.name}' has a successor ({successor.name}). Mark it "
                "`status: deprecated` with a `sunsetDate` (the migration window)"
            )
        if port.status != "deprecated":
            continue
        if port.sunset is None:
            errs.append(f"{where}: deprecated without a `sunsetDate` custom property")
        if successor is None:
            errs.append(f"{where}: deprecated with no successor (`<port>_v<N>`, version_of)")
        readers = products.readers_of(port.domain, port.name)
        if port.sunset is not None and port.sunset <= today:
            errs.append(
                f"{port.name}: sunset {port.sunset} has passed, retire it"
                + (f" after migrating {', '.join(readers)}" if readers else "")
            )
        elif port.sunset is not None:
            left = (port.sunset - today).days
            notes.append(
                f"{port.name} is deprecated, sunset {port.sunset} ({left} days): "
                + (f"still read by {', '.join(readers)}" if readers else "no dbt consumer left")
                + (f", successor {successor.name}" if successor else "")
            )
    return errs, notes


def main() -> int:
    errs, notes = violations()
    ports = products.output_ports()
    in_lifecycle = {p.name for p in products.data_products() if p.version_of}
    in_lifecycle |= {p.name for p in ports.values() if p.status == "deprecated"}
    code = report(
        errs,
        name="Port versions",
        failed="Port versioning violations (ADR-0019)",
        ok=f"{checks.plural(len(ports), 'port')}, {len(in_lifecycle & set(ports))} in a "
        "versioned lifecycle",
        see="governance/standards/breaking-changes.md",
    )
    for notice in notes:
        print(checks.note(f"notice: {notice}"))
    return code


if __name__ == "__main__":
    sys.exit(main())
