"""The ontology exports as one complete Ossie document: structure plus each concept's meaning."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_ossie  # noqa: E402


def test_export_is_a_valid_ossie_document_with_every_value_types_description():
    doc = build_ossie.export()  # validates against Ossie's JSON Schema, or raises

    values = {c["concept"]: c for c in doc["ontology"] if c["type"] == "ValueType"}
    assert values["CustomerKey"]["description"].startswith("Customer key (per order). Rule:")
    assert values["StateCode"]["description"].startswith("Two-letter code")  # a data element
    assert all(c.get("description") for c in values.values())


def test_the_mapping_is_generated_from_the_contracts_links():
    """orders (one row per Order) maps Order by its id and customer key; nothing hand-written."""
    mappings = build_ossie.mappings()  # validated against Ossie's mapping schema, or raises

    (order,) = mappings["orders"]["concept_mappings"]
    assert order["concept"] == "Order"
    assert order["object_mappings"][0]["referent_mappings"] == [
        {"relationship": "has_order_id", "expression": "orders.order_id"}
    ]
    (child,) = order["link_mappings"][0]["children"]
    assert child["object_mapping"] == {"concept": "CustomerKey", "expression": "orders.customer_id"}
    assert "sales_performance" not in mappings  # one row per state is no entity


def test_a_compiled_semantic_model_carries_the_contracts_descriptions():
    """The source omits a contract column's description; the compiled model — what consumers and
    Snowflake read — has it, in the standard Ossie place."""
    (ds,) = build_ossie.semantic_models()["sales_performance"]["datasets"]
    fields = {f["name"]: f for f in ds["fields"]}

    assert fields["num_customers"]["description"].startswith("Distinct customer keys")
    assert fields["customer_state"]["description"].startswith("Two-letter code")


def test_the_bundle_documents_reference_each_other_inside_the_bundle():
    files = build_ossie.bundle()

    mapping = files["orders.mapping.yaml"]
    assert mapping["semantic_model_ref"]["iri"].removeprefix("./") in files
    assert mapping["ontology_ref"]["iri"].removeprefix("./") in files
