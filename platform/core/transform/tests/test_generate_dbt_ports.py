"""dbt sync gets the contract resolved, so an inherited description and classification reach dbt.

Runs the real ``datacontract dbt sync`` (dev group) on a throwaway dbt project.
"""

import importlib.util
import shutil
from pathlib import Path

import pytest
import yaml

# Loaded by path: the module is not importable as a package.
_spec = importlib.util.spec_from_file_location(
    "generate_dbt_ports", Path(__file__).resolve().parents[1] / "generate_dbt_ports.py"
)
generate_dbt_ports = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(generate_dbt_ports)

pytestmark = pytest.mark.skipif(shutil.which("datacontract") is None, reason="datacontract CLI")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    concept = tmp_path / "governance" / "ontology" / "customer_email.yaml"
    concept.parent.mkdir(parents=True)
    concept.write_text(
        yaml.safe_dump(
            {
                "description": "Email address a customer gives us.",
                "classification": "pii",
                "logicalType": "string",
            }
        )
    )
    domain = tmp_path / "domains" / "customer"
    (domain / "contracts").mkdir(parents=True)
    link = [{"type": "semantics", "url": "../../../governance/ontology/customer_email.yaml"}]
    contract = {
        "apiVersion": "v3.1.0",
        "kind": "DataContract",
        "id": "customers",
        "version": "1.0.0",
        "status": "active",
        "schema": [
            {
                "name": "customers",
                "physicalType": "table",
                "tags": ["output_port"],
                "properties": [
                    {
                        "name": "customer_id",
                        "description": "Customer key.",
                        "logicalType": "string",
                    },
                    {"name": "customer_email", "authoritativeDefinitions": link},
                ],
            }
        ],
    }
    (domain / "contracts" / "customers.odcs.yaml").write_text(yaml.safe_dump(contract))
    (domain / "dbt" / "models" / "silver").mkdir(parents=True)
    (domain / "dbt" / "dbt_project.yml").write_text(
        yaml.safe_dump({"name": "dgp_customer", "version": "1.0.0", "profile": "dgp_customer"})
    )
    (domain / "dbt" / "models" / "silver" / "customers.sql").write_text("select 1 as customer_id")
    monkeypatch.setattr(generate_dbt_ports, "REPO_ROOT", tmp_path)
    return tmp_path


def test_dbt_gets_the_inherited_description(repo):
    assert generate_dbt_ports.main() == 0

    ports = yaml.safe_load(
        (repo / "domains/customer/dbt/models/silver/_ports.yml").read_text(encoding="utf-8")
    )
    (model,) = ports["models"]
    columns = {c["name"]: c for c in model["columns"]}
    assert columns["customer_email"]["description"] == "Email address a customer gives us."
    assert columns["customer_id"]["description"] == "Customer key."


def test_the_contract_on_disk_keeps_its_link(repo):
    contract = repo / "domains/customer/contracts/customers.odcs.yaml"
    before = contract.read_text(encoding="utf-8")

    generate_dbt_ports.main()

    assert contract.read_text(encoding="utf-8") == before
