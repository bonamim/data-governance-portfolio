"""Masking and grants read contracts resolved: a column inheriting a maskingPolicy is masked."""

import sys
from pathlib import Path

import pytest
import yaml

ACCESS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ACCESS))
import apply_masking  # noqa: E402
import grant_consumers  # noqa: E402

POLICY = {"method": "hash", "unmask_roles": ["compliance"]}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    concept = tmp_path / "governance" / "ontology" / "customer_email.yaml"
    concept.parent.mkdir(parents=True)
    concept.write_text(
        yaml.safe_dump(
            {
                "description": "Email address a customer gives us.",
                "classification": "pii",
                "customProperties": [{"property": "maskingPolicy", "value": POLICY}],
            }
        )
    )
    contracts = tmp_path / "domains" / "customer" / "contracts"
    contracts.mkdir(parents=True)
    link = [{"type": "semantics", "url": "../../../governance/ontology/customer_email.yaml"}]
    contract = {
        "team": {"name": "customer"},
        "schema": [
            {
                "name": "customers",
                "tags": ["output_port"],
                "properties": [
                    {"name": "customer_id", "classification": "internal"},
                    {"name": "customer_email", "authoritativeDefinitions": link},
                ],
            }
        ],
    }
    (contracts / "customers.odcs.yaml").write_text(yaml.safe_dump(contract))
    monkeypatch.setattr(grant_consumers, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(apply_masking, "REPO_ROOT", tmp_path)
    return tmp_path


def test_a_port_with_an_inherited_masking_policy_is_granted_through_the_masked_view(repo):
    assert grant_consumers.is_masked_port("customer", "customers") is True


def test_masking_plan_masks_the_column_that_inherits_its_policy(repo):
    (plan,) = apply_masking.masking_plan()
    contract, defn, policies = plan

    assert contract.name == "customers.odcs.yaml"
    assert policies == {"customer_email": POLICY}
    assert defn["schema"][0]["properties"][1]["classification"] == "pii"
