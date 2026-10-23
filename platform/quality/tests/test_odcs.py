"""The shared ODCS loader inlines linked definitions, the way datacontract-cli resolves them."""

from pathlib import Path

import pytest
import yaml
from dgp_platform_quality import odcs

REPO = Path(__file__).resolve().parents[3]


def _write(path: Path, doc: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def _contract(*properties: dict, name: str = "customers") -> dict:
    return {
        "apiVersion": "v3.1.0",
        "kind": "DataContract",
        "id": name,
        "version": "1.0.0",
        "status": "active",
        "schema": [{"name": name, "physicalType": "table", "properties": list(properties)}],
    }


def _link(url: str, type_: str = "semantics") -> list[dict]:
    return [{"type": type_, "url": url}]


def _props(doc: dict) -> dict[str, dict]:
    return {p["name"]: p for p in doc["schema"][0]["properties"]}


@pytest.fixture
def ontology(tmp_path):
    _write(
        tmp_path / "ontology" / "email_address.yaml",
        {
            "businessName": "Email address",
            "description": "The address a customer gives us to receive email.",
            "logicalType": "string",
            "classification": "pii",
            "tags": ["personal"],
            "examples": ["ana@example.com"],
        },
    )
    _write(
        tmp_path / "ontology" / "customer_number.yaml",
        {
            "businessName": "Customer number",
            "description": "Stable identifier of a customer, the same in every domain.",
            "logicalType": "string",
            "classification": "internal",
        },
    )
    return tmp_path


def test_a_linked_column_inherits_the_concept(ontology):
    c = _write(
        ontology / "contracts" / "customers.odcs.yaml",
        _contract(
            {"name": "mail", "authoritativeDefinitions": _link("../ontology/email_address.yaml")}
        ),
    )

    mail = _props(odcs.load(c))["mail"]

    assert mail["description"] == "The address a customer gives us to receive email."
    assert mail["classification"] == "pii"
    assert mail["businessName"] == "Email address"
    assert mail["tags"] == ["personal"] and mail["examples"] == ["ana@example.com"]
    assert mail["authoritativeDefinitions"] == _link("../ontology/email_address.yaml")


def test_what_the_contract_sets_itself_wins(ontology):
    c = _write(
        ontology / "contracts" / "orders.odcs.yaml",
        _contract(
            {
                "name": "cust_ref",
                "description": "Customer who placed the order.",
                "classification": "",  # set, even if empty: still the contract's own value
                "authoritativeDefinitions": _link("../ontology/customer_number.yaml"),
            },
            name="orders",
        ),
    )

    cust_ref = _props(odcs.load(c))["cust_ref"]

    assert cust_ref["description"] == "Customer who placed the order."
    assert cust_ref["classification"] == ""
    assert cust_ref["logicalType"] == "string" and cust_ref["businessName"] == "Customer number"


def test_name_id_and_structure_are_never_inherited(tmp_path):
    _write(
        tmp_path / "d.yaml",
        {"id": "x", "name": "other", "description": "d", "properties": [{"name": "n"}]},
    )
    c = _write(
        tmp_path / "c.odcs.yaml",
        _contract({"name": "col", "authoritativeDefinitions": _link("d.yaml")}),
    )

    col = _props(odcs.load(c))["col"]

    assert col["name"] == "col" and "id" not in col and "properties" not in col
    assert col["description"] == "d"


def test_unlinked_columns_and_the_file_on_disk_are_untouched(ontology):
    doc = _contract(
        {"name": "region", "description": "Local.", "classification": "public"},
        {"name": "mail", "authoritativeDefinitions": _link("../ontology/email_address.yaml")},
    )
    c = _write(ontology / "contracts" / "customers.odcs.yaml", doc)

    resolved = odcs.load(c)

    assert _props(resolved)["region"] == {
        "name": "region",
        "description": "Local.",
        "classification": "public",
    }
    assert yaml.safe_load(c.read_text(encoding="utf-8")) == doc


def test_a_column_can_inherit_another_contracts_column(ontology):
    """Physical reuse: the consumer's key takes the producer's definition, which links on."""
    _write(
        ontology / "contracts" / "customers.odcs.yaml",
        _contract(
            {
                "name": "customer_id",
                "id": "customer_id_prop",
                "description": "Per-order customer key.",
                "authoritativeDefinitions": _link("../ontology/customer_number.yaml"),
            }
        ),
    )
    c = _write(
        ontology / "contracts" / "orders.odcs.yaml",
        _contract(
            {
                "name": "customer_id",
                "authoritativeDefinitions": _link(
                    "customers.odcs.yaml#schema/customers/properties/customer_id", "definition"
                ),
            },
            name="orders",
        ),
    )

    col = _props(odcs.load(c))["customer_id"]

    assert col["description"] == "Per-order customer key."  # from the producer's column
    assert col["classification"] == "internal"  # from the concept the producer links to


def test_semantics_wins_over_definition(ontology):
    c = _write(
        ontology / "c.odcs.yaml",
        _contract(
            {
                "name": "mail",
                "authoritativeDefinitions": [
                    {"type": "definition", "url": "ontology/customer_number.yaml"},
                    {"type": "semantics", "url": "ontology/email_address.yaml"},
                ],
            }
        ),
    )

    assert _props(odcs.load(c))["mail"]["classification"] == "pii"


def test_documentation_links_are_left_alone(tmp_path):
    c = _write(
        tmp_path / "c.odcs.yaml",
        _contract(
            {"name": "col", "authoritativeDefinitions": _link("https://wiki/x", "videoTutorial")}
        ),
    )

    assert _props(odcs.load(c))["col"] == {
        "name": "col",
        "authoritativeDefinitions": _link("https://wiki/x", "videoTutorial"),
    }


def test_nested_properties_and_array_items_resolve(ontology):
    c = _write(
        ontology / "c.odcs.yaml",
        _contract(
            {
                "name": "contact",
                "properties": [
                    {
                        "name": "mail",
                        "authoritativeDefinitions": _link("ontology/email_address.yaml"),
                    }
                ],
            },
            {
                "name": "mails",
                "items": {"authoritativeDefinitions": _link("ontology/email_address.yaml")},
            },
        ),
    )

    props = _props(odcs.load(c))

    assert props["contact"]["properties"][0]["classification"] == "pii"
    assert props["mails"]["items"]["classification"] == "pii"


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("https://app.entropy-data.com/org/semantics/main/email", "not a file in the repository"),
        ("/definitions/email", "not a file in the repository"),
        ("missing.yaml", "does not exist"),
        ("other.odcs.yaml#schema/customers", "points at a schema object"),
        ("other.odcs.yaml#schema/customers/properties/nope", "no 'nope'"),
        ("other.odcs.yaml", "is a data contract, not a single property"),
    ],
)
def test_a_link_that_does_not_resolve_rejects_the_contract(tmp_path, url, message):
    _write(tmp_path / "other.odcs.yaml", _contract({"name": "id"}))
    c = _write(
        tmp_path / "c.odcs.yaml", _contract({"name": "col", "authoritativeDefinitions": _link(url)})
    )

    with pytest.raises(odcs.ResolutionError, match=message):
        odcs.load(c)


def test_a_cycle_is_refused(tmp_path):
    _write(tmp_path / "a.yaml", {"description": "a", "authoritativeDefinitions": _link("b.yaml")})
    _write(tmp_path / "b.yaml", {"description": "b", "authoritativeDefinitions": _link("a.yaml")})
    c = _write(
        tmp_path / "c.odcs.yaml",
        _contract({"name": "col", "authoritativeDefinitions": _link("a.yaml")}),
    )

    with pytest.raises(odcs.ResolutionError, match="cycle"):
        odcs.load(c)


def test_resolve_reads_through_a_custom_reader(ontology):
    """How the breaking-change gate resolves a contract as of the base branch."""
    files = {
        ontology / "ontology" / "email_address.yaml": yaml.safe_dump({"classification": "old"}),
    }
    doc = _contract(
        {"name": "mail", "authoritativeDefinitions": _link("ontology/email_address.yaml")}
    )

    resolved = odcs.resolve(doc, ontology / "c.odcs.yaml", read=lambda p: files[p])

    assert _props(resolved)["mail"]["classification"] == "old"


# --- conformance: the same answer as datacontract-cli's own resolver ---------------------------

datacontract_resolve = pytest.importorskip("datacontract.lint.resolve")


def _cli(path: Path) -> dict:
    model = datacontract_resolve.resolve_data_contract(str(path), inline_references=True)
    return model.model_dump(by_alias=True, exclude_none=True, mode="json")


def _cli_properties(doc: dict) -> list[dict]:
    return [p for obj in doc.get("schema") or [] for p in obj.get("properties") or []]


def _same(ours: dict, cli: dict) -> None:
    """Every element the CLI ends up with on a property, ours has, with the same value."""
    for key, value in cli.items():
        if key in ("properties", "items"):
            continue
        assert ours.get(key) == value, f"{ours.get('name')}.{key}: {ours.get(key)!r} != {value!r}"
    for o, c in zip(ours.get("properties") or [], cli.get("properties") or [], strict=True):
        _same(o, c)
    if "items" in cli:
        _same(ours["items"], cli["items"])


def test_conforms_to_datacontract_cli_on_linked_contracts(ontology):
    _write(
        ontology / "contracts" / "customers.odcs.yaml",
        _contract(
            {
                "name": "customer_id",
                "authoritativeDefinitions": _link("../ontology/customer_number.yaml"),
            },
            {
                "name": "mail",
                "description": "Own.",
                "authoritativeDefinitions": _link("../ontology/email_address.yaml"),
            },
            {
                "name": "contact",
                "logicalType": "object",
                "properties": [
                    {
                        "name": "mail",
                        "authoritativeDefinitions": _link("../ontology/email_address.yaml"),
                    }
                ],
            },
        ),
    )
    c = _write(
        ontology / "contracts" / "orders.odcs.yaml",
        _contract(
            {
                "name": "customer_id",
                "authoritativeDefinitions": _link(
                    "customers.odcs.yaml#schema/customers/properties/customer_id", "definition"
                ),
            },
            name="orders",
        ),
    )

    for path in (ontology / "contracts" / "customers.odcs.yaml", c):
        ours, cli = _cli_properties(odcs.load(path)), _cli_properties(_cli(path))
        for o, k in zip(ours, cli, strict=True):
            _same(o, k)


def test_conforms_to_datacontract_cli_on_every_repo_contract():
    paths = sorted(REPO.glob("domains/*/contracts/**/*.odcs.yaml"))
    contracts = [p for p in paths if "definitions" not in p.parts]  # definitions are not contracts
    assert contracts
    for path in contracts:
        ours, cli = _cli_properties(odcs.load(path)), _cli_properties(_cli(path))
        for o, k in zip(ours, cli, strict=True):
            for key in (
                "name",
                "description",
                "classification",
                "businessName",
                "logicalType",
                "tags",
            ):
                assert o.get(key) == k.get(key), f"{path.name}:{o.get('name')}.{key}"


def test_linked_files_lists_every_file_a_contract_reaches(ontology):
    _write(
        ontology / "contracts" / "customers.odcs.yaml",
        _contract(
            {"name": "id", "authoritativeDefinitions": _link("../ontology/customer_number.yaml")}
        ),
    )
    c = _write(
        ontology / "contracts" / "orders.odcs.yaml",
        _contract(
            {
                "name": "cust_ref",
                "authoritativeDefinitions": _link(
                    "customers.odcs.yaml#schema/customers/properties/id", "definition"
                ),
            },
            name="orders",
        ),
    )

    assert odcs.linked_files(c) == {
        (ontology / "contracts" / "customers.odcs.yaml").resolve(),
        (ontology / "ontology" / "customer_number.yaml").resolve(),
    }
