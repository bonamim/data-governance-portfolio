"""The registry reads the ontology: definitions where their domain keeps them, owners derived from
that location, and which definition a column links to."""

from dgp_governance import registry
from dgp_governance.repo import relative, root


def test_a_value_type_is_defined_in_its_owning_domains_folder():
    (path,) = registry.definition_files()["CustomerKey"]

    assert relative(path) == "domains/sales/contracts/dictionary/customer_key.odcs.yaml"
    assert registry.concept_owners()["ZipCodePrefix"] == "reference"


def test_an_entity_type_is_owned_by_the_domain_defining_its_identifier():
    owners = {e["concept"]: e["owner"] for e in registry.entity_types()}

    assert owners == {
        "Customer": "customer",
        "Order": "sales",
        "Review": "customer_experience",
        "Seller": "marketplace",
        "Location": "reference",
        "Product": "marketplace",
    }


def test_a_column_reaches_its_value_type_through_its_data_element():
    """column --businessDefinition--> data element --ontology--> value type (no naming rule)."""
    customers = root() / "domains/customer/contracts/customers.odcs.yaml"

    def link(type_: str, url: str) -> dict:
        return {"authoritativeDefinitions": [{"type": type_, "url": url}]}

    key = link("businessDefinition", "../../sales/contracts/dictionary/customer_key.odcs.yaml")
    assert registry.concept_of(key, customers) == "CustomerKey"
    assert registry.concept_of(link("videoTutorial", "dictionary/x.odcs.yaml"), customers) is None
    assert registry.concept_of({}, customers) is None


def test_every_data_element_names_its_value_type():
    definitions = registry.definition_files()

    assert None not in definitions  # every file has its ontology link
    assert definitions["OrderId"][0].name == "order_id.odcs.yaml"  # keyed by the link
