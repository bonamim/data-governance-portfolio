"""The governance checks judge contracts resolved: inherited meaning counts, a broken link fails."""

import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from dgp_platform_contracts import odcs

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "policies"))
import check_ai_agents  # noqa: E402
import check_breaking_changes  # noqa: E402
import check_output_ports  # noqa: E402
import check_port_versions  # noqa: E402
import check_semantics  # noqa: E402

LINK = "../../../governance/ontology/customer_email.yaml"
CONTRACT = Path("domains/customer/contracts/customers.odcs.yaml")


def _concept(root: Path, **elements) -> None:
    f = root / "governance" / "ontology" / "customer_email.yaml"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(yaml.safe_dump(elements))


def _contract(root: Path, url: str = LINK, version: str = "1.0.0") -> Path:
    contract = {
        "apiVersion": "v3.1.0",
        "kind": "DataContract",
        "id": "customers",
        "version": version,
        "status": "active",
        "schema": [
            {
                "name": "customers",
                "description": "One row per customer.",
                "tags": ["output_port"],
                "properties": [
                    {
                        "name": "customer_id",
                        "description": "Customer key.",
                        "classification": "internal",
                        "logicalType": "string",
                    },
                    {
                        "name": "customer_email",
                        "authoritativeDefinitions": [{"type": "semantics", "url": url}],
                    },
                ],
            }
        ],
    }
    f = root / CONTRACT
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(yaml.safe_dump(contract, sort_keys=False))
    return f


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for module in (check_ai_agents, check_output_ports, check_semantics, check_breaking_changes):
        monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    _concept(
        tmp_path,
        description="Email address a customer gives us.",
        classification="restricted",
        customProperties=[{"property": "privacyCategory", "value": "identifier"}],
    )
    _contract(tmp_path)
    return tmp_path


def test_output_port_completeness_counts_an_inherited_description_and_classification(repo):
    ((domain, _, schema),) = check_output_ports._port_contracts().values()

    assert domain == "customer"
    assert check_output_ports.completeness_violations(schema, "customers") == []


def test_ai_agents_see_a_column_that_inherits_personal_data(repo):

    assert check_ai_agents._personal_columns("customers") == {"customer_email"}


def test_semantics_check_rejects_a_contract_whose_link_is_broken(repo):
    _contract(repo, url="../../../governance/ontology/missing.yaml")

    with pytest.raises(odcs.ResolutionError, match="does not exist"):
        check_semantics._port_columns("customers")


def test_port_versions_read_resolved_contracts(repo):
    assert set(check_port_versions.ports(repo)) == {"customers"}

    _contract(repo, url="https://example.com/semantics/customer_email")
    with pytest.raises(odcs.ResolutionError, match="not a file in the repository"):
        check_port_versions.ports(repo)


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def test_breaking_gate_catches_a_change_made_only_in_a_linked_concept(repo):
    _concept(repo, description="Email.", classification="restricted", logicalType="string")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")

    _concept(repo, description="Email.", classification="restricted", logicalType="integer")

    changed = check_breaking_changes._changed_output_port_contracts("main")
    assert changed == [repo / CONTRACT]  # the contract file itself is unchanged

    old = check_breaking_changes._base_file("main", repo / CONTRACT)
    assert yaml.safe_load(Path(old).read_text())["schema"][0]["properties"][1]["logicalType"] == (
        "string"  # the base side resolves against the concept as it was on main
    )
    report = check_breaking_changes._breaking_report(old, repo / CONTRACT)
    assert report is not None and "customer_email" in report


def test_breaking_gate_ignores_contracts_whose_concepts_did_not_change(repo):
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    (repo / "README.md").write_text("unrelated")

    assert check_breaking_changes._changed_output_port_contracts("main") == []
