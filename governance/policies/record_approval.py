#!/usr/bin/env python3
"""Record a consumer's approval of a breaking change (the approval workflow runs it, ADR-0019).

Adds a team's approval to ``governance/approvals/<port>-v<version>.yaml`` — or, for retiring a port
version (``--version retire``), to ``<port>-retire.yaml`` — creating the file if needed and never
adding a team twice. The breaking-change gate reads the file to unblock the pull request. The
GitHub workflow runs this when an affected consumer approves; it can also be run by hand.
"""

from __future__ import annotations

import argparse
import sys

from dgp_governance import approvals


def _version(value: str) -> approvals.Version:
    """A major version number, or ``retire``."""
    if value == approvals.RETIRE:
        return value
    try:
        return int(value)
    except ValueError as err:
        msg = f"expected a major version or '{approvals.RETIRE}', got {value!r}"
        raise argparse.ArgumentTypeError(msg) from err


def main() -> int:
    p = argparse.ArgumentParser(description="Record a breaking-change approval.")
    p.add_argument("--port", required=True)
    p.add_argument("--version", required=True, type=_version, help="major version, or 'retire'")
    p.add_argument("--team", required=True)
    p.add_argument("--user", default="")
    p.add_argument("--pr", default="")
    a = p.parse_args()

    what = "retirement" if a.version == approvals.RETIRE else f"v{a.version}"
    if approvals.record(a.port, a.version, a.team, a.user, a.pr) is None:
        print(f"{a.team} already recorded for {a.port} {what}")
    else:
        print(f"recorded {a.team} approval for {a.port} {what}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
