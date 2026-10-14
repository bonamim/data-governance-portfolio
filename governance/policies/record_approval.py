#!/usr/bin/env python3
"""Record a consumer's approval of a breaking change (used by the approval workflow, ADR-0019).

Appends a team's approval to ``governance/approvals/<port>-v<version>.yaml`` — creating the file
if needed and never duplicating a team. The breaking-change gate reads this file to unblock the
PR. Run by the GitHub workflow when an affected consumer approves; can also be run by hand.
"""

from __future__ import annotations

import argparse
import datetime
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    p = argparse.ArgumentParser(description="Record a breaking-change approval.")
    p.add_argument("--port", required=True)
    p.add_argument("--version", required=True, type=int)
    p.add_argument("--team", required=True)
    p.add_argument("--user", default="")
    p.add_argument("--pr", default="")
    a = p.parse_args()

    f = REPO_ROOT / "governance" / "approvals" / f"{a.port}-v{a.version}.yaml"
    doc = (yaml.safe_load(f.read_text(encoding="utf-8")) if f.exists() else None) or {
        "port": a.port,
        "version": a.version,
        "approved_by": [],
    }
    doc.setdefault("approved_by", [])
    if any(x.get("team") == a.team for x in doc["approved_by"]):
        print(f"{a.team} already recorded for {a.port} v{a.version}")
        return 0

    entry: dict = {"team": a.team}
    if a.user:
        entry["user"] = a.user
    entry["at"] = datetime.date.today().isoformat()
    if a.pr:
        entry["pr"] = a.pr
    doc["approved_by"].append(entry)

    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    print(f"recorded {a.team} approval for {a.port} v{a.version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
