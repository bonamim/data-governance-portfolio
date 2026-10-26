"""The catalog generator classifies a column by its resolved contract, inherited classification too.

The DataHub SDK is an optional extra (``dgp-platform-catalog[publish]``); only the generator's
contract reading is under test here, so the SDK is stubbed when it is not installed.
"""

import importlib
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml
from dgp_governance import products

SDK = [
    "datahub",
    "datahub.api",
    "datahub.api.entities",
    "datahub.api.entities.dataproduct",
    "datahub.api.entities.dataproduct.dataproduct",
    "datahub.emitter",
    "datahub.emitter.mce_builder",
    "datahub.emitter.mcp",
    "datahub.emitter.rest_emitter",
    "datahub.metadata",
    "datahub.metadata.schema_classes",
]


@pytest.fixture
def generator(monkeypatch):
    try:
        import datahub  # noqa: F401
    except ImportError:
        for name in SDK:
            monkeypatch.setitem(sys.modules, name, MagicMock())
        # The two SDK pieces whose output the tests read: term URNs and the proposal's entity.
        sys.modules["datahub.emitter.mce_builder"].make_term_urn = "urn:li:glossaryTerm:{}".format
        sys.modules["datahub.emitter.mcp"].MetadataChangeProposalWrapper = SimpleNamespace
        schema_classes = sys.modules["datahub.metadata.schema_classes"]
        for name in (
            "GlossaryTermInfoClass",
            "StructuredPropertyDefinitionClass",
            "PropertyValueClass",
        ):
            setattr(schema_classes, name, SimpleNamespace)
    monkeypatch.delitem(sys.modules, "dgp_platform_catalog.publish_governance", raising=False)
    return importlib.import_module("dgp_platform_catalog.publish_governance")


def test_inherited_governed_tags_reach_the_catalog(generator, tmp_repo):
    contracts = tmp_repo / "domains" / "customer" / "contracts"
    (contracts / "ontology").mkdir(parents=True)
    (contracts / "ontology" / "customer_email.yaml").write_text(
        yaml.safe_dump(
            {
                "classification": "restricted",
                "customProperties": [{"property": "privacyCategory", "value": "identifier"}],
            }
        ),
        encoding="utf-8",
    )
    link = [{"type": "semantics", "url": "ontology/customer_email.yaml"}]
    (contracts / "customers.odcs.yaml").write_text(
        yaml.safe_dump(
            {
                "schema": [
                    {
                        "name": "customers",
                        "tags": ["output_port"],
                        "properties": [
                            {"name": "customer_id", "classification": "internal"},
                            {"name": "customer_email", "authoritativeDefinitions": link},
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    assert generator._columns_with_tags(products.output_port("customers")) == [
        ("customer_id", {"sensitivity": "internal"}),
        ("customer_email", {"privacy_category": "identifier", "sensitivity": "restricted"}),
    ]


def test_business_terms_are_glossary_terms_and_data_elements_a_property(generator):
    aspects = generator._ontology_aspects()
    terms = {
        m.entityUrn: m.aspect.parentNode
        for m in aspects
        if getattr(m.aspect, "termSource", None) == "INTERNAL"
    }
    (element,) = [m.aspect for m in aspects if m.entityUrn == generator.DATA_ELEMENT]

    business = "urn:li:glossaryNode:BusinessGlossary"
    assert terms["urn:li:glossaryTerm:Ontology.Order"] == f"{business}.sales"  # an entity type
    assert terms["urn:li:glossaryTerm:Ontology.PaymentAmount"] == f"{business}.sales"  # has a rule
    assert "urn:li:glossaryTerm:Ontology.StateCode" not in terms  # a data element, not a term
    assert {"StateCode", "OrderId", "PaymentAmount"} <= {v.value for v in element.allowedValues}
