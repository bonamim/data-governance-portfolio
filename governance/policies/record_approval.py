#!/usr/bin/env python3
"""Record a consumer's approval of a breaking change (used by the approval workflow, ADR-0019).

Appends a team's approval to ``governance/approvals/<port>-v<version>.yaml`` — or, for a port
version's retirement (``--version retire``), to ``<port>-retire.yaml`` — creating the file if
needed and never duplicating a team. The breaking-change gate reads this file to unblock the PR.
Run by the GitHub workflow when an affected consumer approves; can also be run by hand.
"""

from __future__ import annotations

import argparse
import datetime
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def _version(value: str) -> int | str:
    """A major version number, or ``retire``."""
    if value == "retire":
        return value
    try:
        return int(value)
    except ValueError as err:
        msg = f"expected a major version or 'retire', got {value!r}"
        raise argparse.ArgumentTypeError(msg) from err


def record(
    port: str, version: int | str, team: str, user: str = "", pr: str = "", root: Path = REPO_ROOT
) -> Path | None:
    """Add ``team``'s approval; the approval file, or None if the team was already recorded."""
    retire = version == "retire"
    f = root / "governance" / "approvals" / f"{port}-{'retire' if retire else f'v{version}'}.yaml"
    new = {"port": port, **({"change": "retire"} if retire else {"version": version})}
    doc = (yaml.safe_load(f.read_text(encoding="utf-8")) if f.exists() else None) or new
    doc.setdefault("approved_by", [])
    if any(x.get("team") == team for x in doc["approved_by"]):
        return None

    entry: dict = {"team": team}
    if user:
        entry["user"] = user
    entry["at"] = datetime.date.today().isoformat()
    if pr:
        entry["pr"] = pr
    doc["approved_by"].append(entry)

    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return f


def main() -> int:
    p = argparse.ArgumentParser(description="Record a breaking-change approval.")
    p.add_argument("--port", required=True)
    p.add_argument("--version", required=True, type=_version, help="major version, or 'retire'")
    p.add_argument("--team", required=True)
    p.add_argument("--user", default="")
    p.add_argument("--pr", default="")
    a = p.parse_args()

    what = "retirement" if a.version == "retire" else f"v{a.version}"
    if record(a.port, a.version, a.team, a.user, a.pr) is None:
        print(f"{a.team} already recorded for {a.port} {what}")
    else:
        print(f"recorded {a.team} approval for {a.port} {what}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
