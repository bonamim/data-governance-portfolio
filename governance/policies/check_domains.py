#!/usr/bin/env python3
"""Governance check: the domain registry matches the domains on disk.

The registry (``governance/domains/``) is the single source of truth for the mesh domains.
This fails CI if it drifts from the actual domain folders (``domains/*`` + ``reference/``),
so the two can never disagree.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
from governance.registry import domain_names  # noqa: E402


def _folder_domains() -> set[str]:
    out = {
        p.name
        for p in (REPO_ROOT / "domains").iterdir()
        if p.is_dir() and not p.name.startswith(".")
    }
    if (REPO_ROOT / "reference").is_dir():
        out.add("reference")
    return out


def main() -> int:
    registered = set(domain_names())
    folders = _folder_domains()
    if registered != folders:
        print("Domain registry drift (governance/domains/ vs folders):")
        if registered - folders:
            print(f"  registered with no folder: {sorted(registered - folders)}")
        if folders - registered:
            print(f"  folder not registered:     {sorted(folders - registered)}")
        return 1
    print(f"Domain registry OK: {len(registered)} domain(s), registry matches folders.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
