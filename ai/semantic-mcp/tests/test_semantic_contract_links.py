"""The semantic layer reports a column's classification as the resolved contract has it."""

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import semantic  # noqa: E402


def test_column_classification_includes_what_a_column_inherits(tmp_path, monkeypatch):
    concept = tmp_path / "governance" / "ontology" / "customer_email.yaml"
    concept.parent.mkdir(parents=True)
    concept.write_text(yaml.safe_dump({"classification": "pii"}))
    contracts = tmp_path / "domains" / "customer" / "contracts"
    contracts.mkdir(parents=True)
    link = [{"type": "semantics", "url": "../../../governance/ontology/customer_email.yaml"}]
    contract = {
        "schema": [
            {
                "name": "customers",
                "tags": ["output_port"],
                "properties": [
                    {"name": "customer_state"},
                    {"name": "customer_email", "authoritativeDefinitions": link},
                ],
            }
        ]
    }
    (contracts / "customers.odcs.yaml").write_text(yaml.safe_dump(contract))
    monkeypatch.setattr(semantic, "REPO_ROOT", tmp_path)

    assert semantic._column_classes("customer", "customers") == {
        "customer_state": "unclassified",
        "customer_email": "pii",
    }


def test_columns_of_different_products_with_one_concept_share_it():
    products = {p["data_product"]: p for p in semantic._catalog_products()}

    orders, customers = products["orders"], products["customers"]
    assert orders["column_concepts"]["customer_id"] == "CustomerKey"
    assert customers["column_concepts"]["customer_id"] == "CustomerKey"  # the join key
    assert customers["column_concepts"]["customer_unique_id"] == "CustomerPersonId"
