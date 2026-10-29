"""The catalog generator classifies a column by its resolved contract, inherited classification too.

The DataHub SDK is an optional extra (``dgp-platform-catalog[publish]``). Only the generator's
contract reading is under test here, so the SDK is stubbed when it is not installed.
"""

import importlib
import shutil
import sys
from pathlib import Path
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
        sys.modules["datahub.emitter.mce_builder"].make_tag_urn = "urn:li:tag:{}".format
        sys.modules["datahub.emitter.mce_builder"].make_dataset_urn = (
            lambda platform, name, env="PROD": f"urn:li:dataset:({platform},{name},{env})"
        )
        sys.modules["datahub.emitter.mcp"].MetadataChangeProposalWrapper = SimpleNamespace
        schema_classes = sys.modules["datahub.metadata.schema_classes"]
        for name in (
            "GlossaryTermInfoClass",
            "StructuredPropertyDefinitionClass",
            "PropertyValueClass",
            "EditableSchemaFieldInfoClass",
            "EditableSchemaMetadataClass",
            "EditableDatasetPropertiesClass",
            "GlobalTagsClass",
            "TagAssociationClass",
            "GlossaryTermsClass",
            "GlossaryTermAssociationClass",
        ):
            setattr(schema_classes, name, SimpleNamespace)
    monkeypatch.delitem(sys.modules, "dgp_platform_catalog.publish_governance", raising=False)
    return importlib.import_module("dgp_platform_catalog.publish_governance")


REPO = Path(__file__).resolve().parents[4]


class Recorder:
    """An emitter that keeps what it was given."""

    def __init__(self):
        self.mcps = []

    def emit(self, mcp):
        self.mcps.append(mcp)

    def aspects(self, urn, kind):
        return [m.aspect for m in self.mcps if m.entityUrn == urn and hasattr(m.aspect, kind)]


def _tags(field) -> list[str]:
    return sorted(t.tag for t in field.globalTags.tags)


def _terms(field) -> list[str]:
    return sorted(t.urn for t in field.glossaryTerms.terms)


def test_inherited_governed_tags_reach_the_catalog(generator, tmp_repo):
    shutil.copytree(REPO / "governance" / "ontology", tmp_repo / "governance" / "ontology")
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

    port = products.output_port("customers")
    fields = generator._field_info(
        Recorder(), "urn:port", port.columns, port.contract, set(), tagged=True
    )
    assert {f.fieldPath: _tags(f) for f in fields} == {
        "customer_id": ["urn:li:tag:sensitivity.internal"],
        "customer_email": [
            "urn:li:tag:privacy_category.identifier",
            "urn:li:tag:sensitivity.restricted",
        ],
    }
    # On a warehouse with native tags the asset carries them itself: none from the catalog.
    untagged = generator._field_info(
        Recorder(), "urn:port", port.columns, port.contract, set(), tagged=False
    )
    assert all(_tags(f) == [] for f in untagged)


def test_landing_file_and_raw_table_carry_the_landing_contract(generator):
    """Both assets of a landing dataset get its meaning: the entity's term on the table, and each
    column's business term and governed tags. The landing file is tagged by the catalog whatever
    the warehouse, the raw table only when the warehouse has no native tags."""
    emitter = Recorder()
    generator.publish_ingested(emitter)
    s3 = generator.make_dataset_urn("s3", "landing-archive/accepted/olist/reviews")
    raw = generator.make_dataset_urn(
        generator.WAREHOUSE_PLATFORM, "analytics.customer_experience_raw.reviews"
    )
    for urn in (s3, raw):
        (table,) = emitter.aspects(urn, "terms")
        assert [t.urn for t in table.terms] == ["urn:li:glossaryTerm:Ontology.Review"]
        (schema,) = emitter.aspects(urn, "editableSchemaFieldInfo")
        fields = {f.fieldPath: f for f in schema.editableSchemaFieldInfo}
        assert _tags(fields["review_comment_message"]) == [
            "urn:li:tag:privacy_category.identifier",
            "urn:li:tag:sensitivity.restricted",
        ]
        assert _terms(fields["review_score"]) == ["urn:li:glossaryTerm:Ontology.ReviewScore"]
        assert _terms(fields["order_id"]) == ["urn:li:glossaryTerm:Ontology.Order"]


def test_raw_table_takes_native_tags_on_snowflake(generator, monkeypatch):
    monkeypatch.setattr(generator, "NATIVE_TAGS", True)
    monkeypatch.setattr(generator, "WAREHOUSE_PLATFORM", "snowflake")
    emitter = Recorder()
    generator.publish_ingested(emitter)
    s3 = generator.make_dataset_urn("s3", "landing-archive/accepted/olist/reviews")
    raw = generator.make_dataset_urn("snowflake", "analytics.customer_experience_raw.reviews")
    ((s3_schema,), (raw_schema,)) = (
        emitter.aspects(urn, "editableSchemaFieldInfo") for urn in (s3, raw)
    )
    assert any(_tags(f) for f in s3_schema.editableSchemaFieldInfo)  # object storage: from here
    assert not any(_tags(f) for f in raw_schema.editableSchemaFieldInfo)  # the loader set them
    assert any(_terms(f) for f in raw_schema.editableSchemaFieldInfo)  # terms always from here


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


@pytest.mark.parametrize("ingested", [True, False])
def test_the_dbt_sibling_gets_the_meaning_only_once_ingested(generator, ingested):
    """A raw table that is a dbt source opens as its dbt sibling, so that one carries the meaning
    too, but never before it was ingested: writing to it would create an empty entity."""
    gms = SimpleNamespace(has_aspect=lambda urn, aspect: ingested)
    emitter = Recorder()
    generator.publish_ingested(emitter, gms)
    dbt = generator.make_dataset_urn("dbt", "analytics.customer_experience_raw.reviews")
    assert bool(emitter.aspects(dbt, "editableSchemaFieldInfo")) is ingested
    if ingested:
        # The dbt source describes no warehouse object, so the catalog carries the descriptions.
        (table,) = emitter.aspects(dbt, "description")
        assert table.description.startswith("One row per survey answer and order")
        (schema,) = emitter.aspects(dbt, "editableSchemaFieldInfo")
        fields = {f.fieldPath: f for f in schema.editableSchemaFieldInfo}
        assert fields["review_comment_message"].description.startswith("The comment the customer")
        assert fields["_system_loaded_at"].description.startswith("When the load")
    # The warehouse table's descriptions come from the loader's COMMENTs, through its ingest.
    pg = generator.make_dataset_urn(
        generator.WAREHOUSE_PLATFORM, "analytics.customer_experience_raw.reviews"
    )
    (pg_schema,) = emitter.aspects(pg, "editableSchemaFieldInfo")
    assert all(f.description is None for f in pg_schema.editableSchemaFieldInfo)


def test_every_raw_table_carries_the_system_columns_but_no_landing_file(generator):
    """The platform's columns are added by the loader, so they are on each raw table, an OLTP
    source's too, and never on the landing file: no source delivers them (ADR-0028)."""
    emitter = Recorder()
    generator.publish_ingested(emitter)
    s3 = generator.make_dataset_urn("s3", "landing-archive/accepted/olist/reviews")
    landing_raw = generator.make_dataset_urn(
        generator.WAREHOUSE_PLATFORM, "analytics.customer_experience_raw.reviews"
    )
    oltp_raw = generator.make_dataset_urn(
        generator.WAREHOUSE_PLATFORM, "analytics.sales_raw.orders"
    )

    def columns(urn):
        (schema,) = emitter.aspects(urn, "editableSchemaFieldInfo")
        return {f.fieldPath: f for f in schema.editableSchemaFieldInfo}

    assert not any(c.startswith("_system_") for c in columns(s3))
    assert "_system_batch_id" in columns(landing_raw)
    assert set(columns(oltp_raw)) == {
        "_system_batch_id",
        "_system_source_path",
        "_system_partition",
        "_system_loaded_at",
    }
    assert _tags(columns(oltp_raw)["_system_loaded_at"]) == ["urn:li:tag:sensitivity.internal"]
