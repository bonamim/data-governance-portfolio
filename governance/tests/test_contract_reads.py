"""Every reader of an ODCS contract goes through the shared loader (dgp_platform_contracts.odcs), so
no reader sees a contract without the meaning its columns inherit."""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCAN = ["platform", "governance/policies", "governance/src", "ai", "proofs"]
SKIP = {".venv", "__pycache__", "tests", "target", "dbt_packages"}
USES_LOADER = re.compile(
    r"from dgp_platform_(?:contracts import .*\bodcs|quality\.rules import .*\bload_contract)\b"
)
# Files that name contracts without reading their meaning — each with the reason.
NOT_READERS = {
    "proofs/p18_completion_and_sla.py": "edits the contract text to provoke a failure",
    "proofs/p19_versioned_port.py": "copies a contract to a new version (links must stay links)",
}


def _files_naming_contracts() -> list[Path]:
    out = []
    for d in SCAN:
        for f in (REPO / d).rglob("*.py"):
            if SKIP & set(f.relative_to(REPO).parts):
                continue
            if f.name != "odcs.py" and "odcs.yaml" in f.read_text(encoding="utf-8"):
                out.append(f)
    return out


def test_contract_readers_use_the_shared_loader():
    files = _files_naming_contracts()
    assert files
    raw = [
        f.relative_to(REPO).as_posix()
        for f in files
        if f.relative_to(REPO).as_posix() not in NOT_READERS
        and not USES_LOADER.search(f.read_text(encoding="utf-8"))
    ]
    assert raw == [], f"read contracts with dgp_platform_contracts.odcs.load: {raw}"


def test_the_exceptions_are_still_needed():
    named = {f.relative_to(REPO).as_posix() for f in _files_naming_contracts()}
    assert set(NOT_READERS) <= named, set(NOT_READERS) - named
