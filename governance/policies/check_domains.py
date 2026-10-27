#!/usr/bin/env python3
"""The domain registry matches the domain folders.

The registry (``governance/domains/``) is the single source of truth for the mesh's domains. This
fails CI if it drifts from the folders under ``domains/``, so the two can never disagree.
"""

from __future__ import annotations

import sys

from _checks import report
from dgp_governance import checks, registry
from dgp_governance.repo import root


def violations() -> list[str]:
    registered = set(registry.domain_names())
    folders = {
        p.name for p in (root() / "domains").iterdir() if p.is_dir() and not p.name.startswith(".")
    }
    out = [f"'{d}' is registered but has no folder under domains/" for d in registered - folders]
    out += [f"domains/{d}/ has no registry entry" for d in folders - registered]
    return sorted(out)


def main() -> int:
    return report(
        violations(),
        name="Domains",
        failed="Domain registry drift (governance/domains/ vs domains/)",
        ok=f"{checks.plural(len(registry.domain_names()), 'domain')}, registry matches folders",
    )


if __name__ == "__main__":
    sys.exit(main())
