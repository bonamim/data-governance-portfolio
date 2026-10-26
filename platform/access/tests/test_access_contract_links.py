"""Masking and grants read contracts resolved: a column inheriting a maskingPolicy is masked."""

import pytest
import yaml
from dgp_governance import products
from dgp_platform_access import apply_grants, apply_masking

POLICY = {"method": "hash", "unmask_roles": ["compliance"]}


@pytest.fixture
def repo(tmp_repo):
    concept = tmp_repo / "governance" / "ontology" / "customer_email.yaml"
    concept.parent.mkdir(parents=True)
    concept.write_text(
        yaml.safe_dump(
            {
                "description": "Email address a customer gives us.",
                "classification": "pii",
                "customProperties": [{"property": "maskingPolicy", "value": POLICY}],
            }
        ),
        encoding="utf-8",
    )
    contracts = tmp_repo / "domains" / "customer" / "contracts"
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
    (contracts / "customers.odcs.yaml").write_text(yaml.safe_dump(contract), encoding="utf-8")
    return tmp_repo


def test_a_port_with_an_inherited_masking_policy_is_granted_through_the_masked_view(repo):
    assert apply_grants.is_masked(products.output_port("customers")) is True


def test_masking_plan_masks_the_column_that_inherits_its_policy(repo):
    ((port, policies),) = apply_masking.masking_plan()

    assert port.contract.name == "customers.odcs.yaml"
    assert policies == {"customer_email": POLICY}
    assert port.columns[1]["classification"] == "pii"
