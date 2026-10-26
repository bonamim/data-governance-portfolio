"""Consumers' sign-off on a breaking change to an output port, or on retiring one (ADR-0019).

An approval is a file in git: ``governance/approvals/<port>-v<major>.yaml`` for a new major
version, ``<port>-retire.yaml`` for retiring a port version. The approval workflow appends a team
when it approves the pull request, and it can be committed by hand. The file is the authority;
GitHub is only the automation. The breaking-change gate reads it to unblock the change.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import yaml

from .repo import load_yaml, root

RETIRE = "retire"

Version = int | str  # a major version number, or RETIRE


def path(port: str, version: Version, base: Path | None = None) -> Path:
    """Where the approvals for this change are recorded."""
    suffix = RETIRE if version == RETIRE else f"v{version}"
    return (base or root()) / "governance" / "approvals" / f"{port}-{suffix}.yaml"


def approved_teams(port: str, version: Version, base: Path | None = None) -> set[str]:
    """The teams recorded as approving this change."""
    file = path(port, version, base)
    if not file.exists():
        return set()
    return {a["team"] for a in load_yaml(file).get("approved_by") or [] if a.get("team")}


def record(
    port: str, version: Version, team: str, user: str = "", pr: str = "", base: Path | None = None
) -> Path | None:
    """Add ``team``'s approval; returns the approval file, or None if the team was already in it."""
    file = path(port, version, base)
    header = {"port": port, **({"change": RETIRE} if version == RETIRE else {"version": version})}
    doc = (load_yaml(file) if file.exists() else None) or header
    approvals = doc.setdefault("approved_by", [])
    if any(a.get("team") == team for a in approvals):
        return None

    entry: dict = {"team": team}
    if user:
        entry["user"] = user
    entry["at"] = datetime.date.today().isoformat()
    if pr:
        entry["pr"] = pr
    approvals.append(entry)

    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return file
