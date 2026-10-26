"""The semantic layer reports a column's classification as the resolved contract has it."""

import yaml
from dgp_ai import semantic
from dgp_governance import products


def test_column_classification_includes_what_a_column_inherits(tmp_repo):
    concept = tmp_repo / "governance" / "ontology" / "customer_email.yaml"
    concept.parent.mkdir(parents=True)
    concept.write_text(yaml.safe_dump({"classification": "pii"}), encoding="utf-8")
    contracts = tmp_repo / "domains" / "customer" / "contracts"
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
    (contracts / "customers.odcs.yaml").write_text(yaml.safe_dump(contract), encoding="utf-8")

    assert semantic.column_classification(products.output_port("customers")) == {
        "customer_state": "unclassified",
        "customer_email": "pii",
    }


def test_columns_of_different_products_with_one_concept_share_it():
    catalog = {p["data_product"]: p for p in semantic.SemanticLayer().list_data_products()}

    orders, customers = catalog["orders"], catalog["customers"]
    assert orders["column_concepts"]["customer_id"] == "CustomerKey"
    assert customers["column_concepts"]["customer_id"] == "CustomerKey"  # the join key
    assert customers["column_concepts"]["customer_unique_id"] == "CustomerPersonId"
