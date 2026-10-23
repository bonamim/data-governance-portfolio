"""The ontology policy (ADR-0027): a valid Ossie document, definitions owned by the domain whose
folder holds them, and one definition per column across every contract."""

import copy
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "policies"))
import check_ontology  # noqa: E402

DOMAINS = {"customer", "sales"}
CLASSES = {"restricted", "confidential", "internal", "public"}
ONTOLOGY = {
    "version": "0.2.0.dev0",
    "name": "shop",
    "ontology": [
        {
            "concept": "Order",
            "type": "EntityType",
            "description": "One purchase. A valid order is not canceled. Used for: order volume.",
            "identify_by": ["has_order_id"],
            "relationships": [
                {
                    "name": "has_order_id",
                    "roles": [{"concept": "OrderId"}],
                    "multiplicity": "OneToOne",
                    "verbalizes": ["{Order} is identified by {OrderId}"],
                }
            ],
        },
        {"concept": "OrderId", "type": "ValueType", "extends": ["String"]},
    ],
}
ORDER_ID = {
    "businessName": "Order id",
    "description": "Identifies one order.",
    "logicalType": "string",
    "classification": "internal",
}


def _define(root: Path, domain: str, term: str, meaning: dict) -> Path:
    """A data element file naming its value type (PascalCase of ``term``) with an ontology link."""
    onto = root / "governance" / "ontology" / "commerce.ontology.yaml"
    onto.parent.mkdir(parents=True, exist_ok=True)
    onto.touch()
    value_type = "".join(w.title() for w in term.split("_"))
    link = {
        "type": "ontology",
        "url": f"../../../../governance/ontology/commerce.ontology.yaml#{value_type}",
    }
    f = root / "domains" / domain / "contracts" / "definitions" / f"{term}.odcs.yaml"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(yaml.safe_dump({**meaning, "authoritativeDefinitions": [link]}))
    return f


def _violations(root: Path, onto=None, definitions=None) -> list[str]:
    if definitions is None:
        definitions = {"OrderId": [_define(root, "sales", "order_id", ORDER_ID)]}
    return check_ontology.ontology_violations(
        onto or ONTOLOGY, definitions, DOMAINS, CLASSES, root=root
    )


def _relationship(name: str, concept: str) -> dict:
    return {
        "name": name,
        "roles": [{"concept": concept}],
        "multiplicity": "ManyToOne",
        "verbalizes": [f"{{Order}} {name} {{{concept}}}"],
    }


def test_the_repository_ontology_and_contracts_pass(capsys):
    assert check_ontology.main() == 0
    assert "valid Ossie document" in capsys.readouterr().out


def test_a_well_formed_ontology_passes(tmp_path):
    assert _violations(tmp_path) == []


def test_ossies_own_validator_judges_the_document(tmp_path):
    onto = copy.deepcopy(ONTOLOGY)
    onto["ontology"][0]["relationships"][0]["multiplicity"] = "OneToMany"  # not an Ossie value

    assert any("OneToMany" in v for v in _violations(tmp_path, onto=onto))


def test_a_reference_outside_the_one_document_fails_ossies_validator(tmp_path):
    onto = copy.deepcopy(ONTOLOGY)
    onto["ontology"][0]["relationships"].append(_relationship("placed_by", "Customer"))

    assert any("unknown concept 'Customer'" in v for v in _violations(tmp_path, onto=onto))


def test_an_entity_type_is_a_business_term_with_a_business_definition(tmp_path):
    onto = copy.deepcopy(ONTOLOGY)
    del onto["ontology"][0]["description"]

    found = _violations(tmp_path, onto=onto)

    assert any("entity type 'Order': needs a business definition" in v for v in found)


def test_ownership_comes_from_the_folder_that_defines_the_identifier(tmp_path):
    misplaced = {"OrderId": [_define(tmp_path, "nowhere", "order_id", ORDER_ID)]}

    found = _violations(tmp_path, definitions=misplaced)

    assert any("defined by one registered domain (found ['nowhere'])" in v for v in found)


def test_an_entity_types_value_types_are_defined_by_its_owner(tmp_path):
    onto = copy.deepcopy(ONTOLOGY)
    onto["ontology"][0]["relationships"].append(_relationship("has_status", "OrderStatus"))
    onto["ontology"].append({"concept": "OrderStatus", "type": "ValueType", "extends": ["String"]})
    status = {**ORDER_ID, "businessName": "Order status", "description": "Lifecycle status."}
    definitions = {
        "OrderId": [_define(tmp_path, "sales", "order_id", ORDER_ID)],
        "OrderStatus": [_define(tmp_path, "customer", "order_status", status)],
    }

    found = _violations(tmp_path, onto=onto, definitions=definitions)

    assert any("'OrderStatus' belongs to entity type 'Order' (domain sales)" in v for v in found)


def test_value_types_and_definitions_match_one_to_one(tmp_path):
    definitions = {
        "OrderId": [
            _define(tmp_path, "sales", "order_id", ORDER_ID),
            _define(tmp_path, "customer", "order_id", ORDER_ID),
        ],
        "Coupon": [_define(tmp_path, "sales", "coupon", ORDER_ID)],
    }

    found = _violations(tmp_path, definitions=definitions)

    assert any("'OrderId' is defined 2 times" in v for v in found)
    assert any("definition 'Coupon': no value type" in v for v in found)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"physicalType": "text", "required": True}, "table-specific"),
        ({"classification": "secret"}, "classification must be one of"),
        ({"logicalType": "integer"}, "disagrees with the ontology (extends String, so string)"),
        ({"description": " "}, "needs a description"),
        ({"colour": "red"}, "unknown ODCS property fields ['colour']"),
    ],
)
def test_a_definition_carries_valid_meaning_only(tmp_path, change, message):
    definitions = {"OrderId": [_define(tmp_path, "sales", "order_id", {**ORDER_ID, **change})]}

    assert any(message in v for v in _violations(tmp_path, definitions=definitions))


# --- contracts ----------------------------------------------------------------------------------


def _link(term: str = "order_id") -> list[dict]:
    return [{"type": "businessDefinition", "url": f"definitions/{term}.odcs.yaml"}]


def _contract(root: Path, i: int, *columns: dict) -> None:
    f = root / "domains" / "sales" / "contracts" / f"port{i}.odcs.yaml"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(yaml.safe_dump({"schema": [{"name": f"port{i}", "properties": list(columns)}]}))


def _repo(root: Path, *columns_by_contract: list[dict]) -> dict[str, list[Path]]:
    definitions = {"OrderId": [_define(root, "sales", "order_id", ORDER_ID)]}
    for i, columns in enumerate(columns_by_contract):
        _contract(root, i, *columns)
    return definitions


def _contract_violations(root: Path, definitions) -> list[str]:
    return check_ontology.contract_violations(root, definitions)[0]


def test_linked_columns_across_contracts_pass(tmp_path):
    definitions = _repo(
        tmp_path,
        [{"name": "order_id", "authoritativeDefinitions": _link(), "physicalType": "text"}],
        [{"name": "order_id", "authoritativeDefinitions": _link(), "classification": "public"}],
    )

    assert _contract_violations(tmp_path, definitions) == []  # overriding classification is fine


@pytest.mark.parametrize(
    ("local", "message"),
    [
        ({"description": "Identifies one order."}, "restates its definition's description"),
        ({"description": ""}, "sets an empty description"),
        ({"logicalType": "integer"}, "is logicalType integer, but definition 'OrderId' is string"),
    ],
)
def test_one_place_per_fact(tmp_path, local, message):
    column = {"name": "order_id", "authoritativeDefinitions": _link(), **local}
    definitions = _repo(tmp_path, [column])

    assert any(message in v for v in _contract_violations(tmp_path, definitions))


@pytest.mark.parametrize("type_", ["businessDefinition", "semantics", "definition"])
def test_a_column_takes_its_meaning_only_from_a_definition(tmp_path, type_):
    (tmp_path / "elsewhere.yaml").write_text(yaml.safe_dump({"description": "x"}))
    link = [{"type": type_, "url": "../../../elsewhere.yaml"}]
    definitions = _repo(tmp_path, [{"name": "order_id", "authoritativeDefinitions": link}])

    found = _contract_violations(tmp_path, definitions)

    assert any("takes its meaning from outside the definitions" in v for v in found)


def test_a_broken_link_is_reported(tmp_path):
    column = {"name": "order_id", "authoritativeDefinitions": _link("missing")}
    definitions = _repo(tmp_path, [column])

    assert any("does not exist" in v for v in _contract_violations(tmp_path, definitions))


def test_a_shared_column_name_is_linked_everywhere(tmp_path):
    definitions = _repo(
        tmp_path,
        [{"name": "order_id", "authoritativeDefinitions": _link()}],
        [{"name": "order_id", "description": "Our own copy."}],
    )

    found = _contract_violations(tmp_path, definitions)

    assert any("'order_id' appears in several contracts but is not linked" in v for v in found)


def test_a_homonym_is_explicit_two_definitions_under_one_name(tmp_path):
    """`status` of an order and of a payment: same name, each column links its own meaning."""
    definitions = _repo(tmp_path, [{"name": "order_id", "authoritativeDefinitions": _link()}])
    for i, (term, value_type) in enumerate(
        (("order_status", "OrderStatus"), ("payment_status", "PaymentStatus")), start=1
    ):
        meaning = {**ORDER_ID, "businessName": term, "description": f"The {term}."}
        definitions[value_type] = [_define(tmp_path, "sales", term, meaning)]
        _contract(tmp_path, i, {"name": "status", "authoritativeDefinitions": _link(term)})

    violations, homonyms = check_ontology.contract_violations(tmp_path, definitions)

    assert violations == []
    assert homonyms == {"status": ["OrderStatus", "PaymentStatus"]}


def test_an_unused_definition_is_reported(tmp_path):
    definitions = _repo(tmp_path, [{"name": "order_number", "description": "Not linked."}])

    found = _contract_violations(tmp_path, definitions)

    assert any("definition 'OrderId': no contract column links to it" in v for v in found)


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("../../../governance/ontology/commerce.ontology.yaml#OrderId", "not an entity type"),
        ("../../../nowhere.ontology.yaml#Order", "does not resolve"),
    ],
)
def test_a_table_names_the_entity_type_it_represents(tmp_path, url, message):
    onto = tmp_path / "governance" / "ontology" / "commerce.ontology.yaml"
    onto.parent.mkdir(parents=True, exist_ok=True)
    onto.write_text("name: shop\n")
    definitions = _repo(tmp_path, [{"name": "order_id", "authoritativeDefinitions": _link()}])
    f = tmp_path / "domains" / "sales" / "contracts" / "port0.odcs.yaml"
    doc = yaml.safe_load(f.read_text())
    doc["schema"][0]["authoritativeDefinitions"] = [{"type": "ontology", "url": url}]
    f.write_text(yaml.safe_dump(doc))

    assert any(message in v for v in _contract_violations(tmp_path, definitions))
