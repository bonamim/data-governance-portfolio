"""Definitions work the same when each domain has its own repository (ADR-0027).

Every repository keeps the monorepo's paths (``domains/<domain>/…``), and a domain
vendors the definitions it reuses from other domains, read-only, at their owner's path — as it
vendors their dbt manifests for dbt-loom. Then a contract's relative links resolve unchanged.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
from dgp_platform_quality import odcs

REPO = Path(__file__).resolve().parents[2]
CUSTOMERS = Path("domains/customer/contracts/customers.odcs.yaml")
OWN = Path("domains/customer/contracts/definitions")
REUSED = [
    Path("domains/sales/contracts/definitions"),
    Path("domains/reference/contracts/definitions"),
]


def _customer_repo(root: Path, vendored: list[Path]) -> Path:
    """A repository holding only the customer domain, plus the vendored definitions."""
    (root / CUSTOMERS).parent.mkdir(parents=True)
    shutil.copy(REPO / CUSTOMERS, root / CUSTOMERS)
    shutil.copytree(REPO / OWN, root / OWN)
    for rel in vendored:
        shutil.copytree(REPO / rel, root / rel)
    return root / CUSTOMERS


def _columns(contract: dict) -> dict[str, dict]:
    return {p["name"]: p for p in contract["schema"][0]["properties"]}


def test_a_domain_repository_resolves_its_contracts_with_vendored_definitions(tmp_path):
    contract = _columns(odcs.load(_customer_repo(tmp_path, REUSED)))

    assert contract["customer_id"]["businessName"] == "Customer key (per order)"  # sales' meaning
    assert contract["customer_unique_id"]["classification"] == "restricted"  # the domain's own
    assert contract["customer_state"]["businessName"] == "State"  # reference's


def test_a_missing_vendored_definition_is_a_clear_error_not_a_silent_gap(tmp_path):
    path = _customer_repo(tmp_path, [Path("domains/reference/contracts/definitions")])  # no sales

    with pytest.raises(odcs.ResolutionError, match="customer_key.odcs.yaml' does not exist"):
        odcs.load(path)


@pytest.mark.skipif(shutil.which("datacontract") is None, reason="datacontract CLI")
def test_datacontract_cli_resolves_the_domain_repository_too(tmp_path):
    path = _customer_repo(tmp_path, REUSED)

    r = subprocess.run(
        [shutil.which("datacontract"), "export", "odcs", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert r.returncode == 0, r.stdout + r.stderr
    assert "Customer key (per order)" in r.stdout
