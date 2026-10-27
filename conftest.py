"""Shared test fixtures."""

import shutil
from pathlib import Path

import pytest
from dgp_governance.repo import ROOT_ENV

REPO = Path(__file__).resolve().parent


@pytest.fixture
def tmp_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway repository the code under test works on (``DGP_REPO_ROOT``), seeded with the
    real domain registry and governed tags. A test adds the contracts, products and models it
    needs."""
    for registry in ("domains", "tags"):
        shutil.copytree(REPO / "governance" / registry, tmp_path / "governance" / registry)
    monkeypatch.setenv(ROOT_ENV, str(tmp_path))
    return tmp_path
