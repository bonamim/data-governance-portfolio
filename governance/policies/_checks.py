"""What the policy checks share: how they report, and how they lint a contract.

Every check prints the same way and exits the same way (0 when the rules hold, 1 with one line per
violation when they don't), so CI logs read alike whichever policy failed.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

from dgp_governance import checks


def report(
    violations: Sequence[str], *, name: str, failed: str, ok: str, see: str | None = None
) -> int:
    """Print the outcome of a check (one aligned line when it passes) and return its exit code."""
    if not violations:
        print(checks.passed(name, ok))
        return 0
    print(checks.failed(name, f"{failed}:") + "\n")
    for violation in violations:
        print(f"  - {violation}")
    print(f"\n{len(violations)} violation(s)." + (f" See {see}." if see else ""))
    return 1


def lint_contract(path: Path) -> tuple[bool, str]:
    """``datacontract lint`` on one contract: (valid, the CLI's last line of output)."""
    datacontract = shutil.which("datacontract")
    if datacontract is None:
        return False, "datacontract CLI not found (it is in the dev dependency group)"
    proc = subprocess.run([datacontract, "lint", str(path)], capture_output=True, text=True)
    output = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, output.splitlines()[-1] if output else ""
