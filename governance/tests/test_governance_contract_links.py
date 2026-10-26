"""The governance checks judge contracts resolved: inherited meaning counts, a broken link fails."""

import subprocess
from pathlib import Path

import check_ai_agents
import check_breaking_changes
import check_output_ports
import check_port_versions
import check_semantics
import pytest
import yaml
from dgp_governance import products
from dgp_platform_contracts import odcs

LINK = "../../../governance/ontology/customer_email.yaml"
CONTRACT = Path("domains/customer/contracts/customers.odcs.yaml")


def _concept(root: Path, **elements) -> None:
    f = root / "governance" / "ontology" / "customer_email.yaml"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(yaml.safe_dump(elements), encoding="utf-8")


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
    f.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    return f


@pytest.fixture
def repo(tmp_repo):
    _concept(
        tmp_repo,
        description="Email address a customer gives us.",
        classification="restricted",
        customProperties=[{"property": "privacyCategory", "value": "identifier"}],
    )
    _contract(tmp_repo)
    return tmp_repo


def test_output_port_completeness_counts_an_inherited_description_and_classification(repo):
    (port,) = products.output_ports().values()

    assert port.domain == "customer"
    assert check_output_ports.completeness_violations(port.schema, "customers") == []


def test_ai_agents_see_a_column_that_inherits_personal_data(repo):
    assert check_ai_agents.personal_columns("customers") == {"customer_email"}


def test_semantics_check_rejects_a_contract_whose_link_is_broken(repo):
    _contract(repo, url="../../../governance/ontology/missing.yaml")
    model = repo / "domains" / "customer" / "semantics" / "customers.semantic_model.yaml"
    model.parent.mkdir(parents=True)
    model.write_text(
        yaml.safe_dump({"name": "customers", "datasets": [{"source": "customers"}]}),
        encoding="utf-8",
    )

    with pytest.raises(odcs.ResolutionError, match="does not exist"):
        check_semantics.model_violations(model)


def test_port_versions_read_resolved_contracts(repo):
    assert check_port_versions.violations() == ([], [])

    _contract(repo, url="https://example.com/semantics/customer_email")
    with pytest.raises(odcs.ResolutionError, match="not a file in the repository"):
        check_port_versions.violations()


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
    assert yaml.safe_load(Path(old).read_text(encoding="utf-8"))["schema"][0]["properties"][1][
        "logicalType"
    ] == (
        "string"  # the base side resolves against the concept as it was on main
    )
    report = check_breaking_changes._breaking_report(old, repo / CONTRACT)
    assert report is not None and "customer_email" in report


def test_breaking_gate_ignores_contracts_whose_concepts_did_not_change(repo):
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    (repo / "README.md").write_text("unrelated", encoding="utf-8")

    assert check_breaking_changes._changed_output_port_contracts("main") == []
