"""Every script imports cleanly: a script whose imports broke would otherwise fail only when it is
next run for real (a restore drill, a proof against the live platform).

Each script is imported in a fresh interpreter started in its own folder, which puts that folder
first on the path just as running the script does, so one script's modules can't satisfy
another's imports. Importing runs no ``main()``: every script guards it with
``if __name__ == "__main__"``.
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT_DIRS = [
    "governance/policies",
    "platform/context/semantics",
    "platform/core/infra",
    "platform/core/recovery",
    "platform/core/transform",
    "proofs",
    "ai/eval",
]
SCRIPTS = sorted(p for d in SCRIPT_DIRS for p in (REPO / d).glob("*.py"))
IMPORT = "import runpy, sys; runpy.run_path(sys.argv[1])"


def test_every_script_folder_has_scripts():
    assert {p.parent.relative_to(REPO).as_posix() for p in SCRIPTS} == set(SCRIPT_DIRS)


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.relative_to(REPO).as_posix())
def test_script_imports(script: Path):
    # run_path with its default run_name ("<run_path>") executes the module body, not main().
    proc = subprocess.run(
        [sys.executable, "-c", IMPORT, str(script)],
        cwd=script.parent,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
