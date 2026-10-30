"""Push the *governance* layer into DataHub from our source-of-truth definitions.

Technical metadata (datasets, lineage, tests) comes from the dbt/Postgres ingestion recipes.
This adds the mesh governance on top, generated (not hand-maintained in the UI) from the same
definitions that drive grants and the data contracts:

  * **Domains**: one per mesh domain. Every warehouse/dbt dataset is filed under its domain.
  * **Data products**: one per ``domains/*/data_products/*.yaml``: display name, purpose,
    owner, and the output-port datasets (Postgres + dbt sibling) as its assets.
  * **Meaning**: the business glossary and the data dictionary from the ontology (ADR-0027), and
    on every contracted asset (each output port, and each landing file with the raw table it
    loads into) the entity's term on the table and, on every column, its data element, business
    term and governed tags. Every raw table's system columns too (ADR-0028).
  * **Links**: each warehouse dataset to the Airflow DAG that builds it, and each BI dataset to
    the output port it reads (lineage through to the chart, proof P7).

Run: ``make catalog-govern``. Idempotent: re-running upserts.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from datahub.api.entities.dataproduct.dataproduct import DataProduct
from datahub.emitter.mce_builder import (
    make_dataset_urn,
    make_domain_urn,
    make_group_urn,
    make_schema_field_urn,
    make_tag_urn,
    make_term_urn,
)
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.rest_emitter import DatahubRestEmitter
from datahub.metadata.schema_classes import (
    AuditStampClass,
    DatasetLineageTypeClass,
    DeprecationClass,
    DomainPropertiesClass,
    DomainsClass,
    EditableSchemaFieldInfoClass,
    EditableSchemaMetadataClass,
    GlobalTagsClass,
    GlossaryNodeInfoClass,
    GlossaryRelatedTermsClass,
    GlossaryTermAssociationClass,
    GlossaryTermInfoClass,
    GlossaryTermsClass,
    InstitutionalMemoryClass,
    InstitutionalMemoryMetadataClass,
    OwnerClass,
    OwnershipClass,
    OwnershipTypeClass,
    PropertyCardinalityClass,
    PropertyValueClass,
    StructuredPropertiesClass,
    StructuredPropertyDefinitionClass,
    StructuredPropertyValueAssignmentClass,
    TagAssociationClass,
    TagPropertiesClass,
    UpstreamClass,
    UpstreamLineageClass,
)
from dgp_governance import products, registry
from dgp_governance.repo import root
from dgp_platform_contracts import odcs
from dgp_platform_ingestion import layout
from dgp_platform_ingestion.config import load_datasets
from dgp_platform_quality.rules import contract_path
from dgp_platform_semantics import ossie

from .gms import Gms, default_url

DB = os.environ.get("ANALYTICS_DB", "analytics")
# The warehouse's DataHub platform: postgres locally, snowflake/bigquery/etc. in the cloud.
WAREHOUSE_PLATFORM = os.environ.get("WAREHOUSE_PLATFORM", "postgres")
AIRFLOW_URL = os.environ.get(
    "AIRFLOW_BASE_URL", f"http://localhost:{os.environ.get('AIRFLOW_API_PORT', '8080')}"
)
BUSINESS_NODE = "urn:li:glossaryNode:BusinessGlossary"
DATA_ELEMENT = "urn:li:structuredProperty:dgp.data_element"
ARCHIVE = os.environ.get("LANDING_ARCHIVE_BUCKET", "landing-archive")
# A warehouse with native tags carries the governed tags itself (the dbt build sets them on a port,
# the loader on a raw table) and DataHub ingests them from there. A warehouse without tags
# (Postgres) gets them from here, and so does a landing file: object storage has none.
NATIVE_TAGS = WAREHOUSE_PLATFORM in {"snowflake", "databricks", "bigquery"}
AUDIT = AuditStampClass(time=0, actor="urn:li:corpuser:datahub")


def _domain_of(schema: str, domains: list[str]) -> str | None:
    """The mesh domain of a warehouse schema (``<domain>_<layer>``). The longest match wins."""
    for d in sorted(domains, key=len, reverse=True):
        if schema == d or schema.startswith(d + "_"):
            return d
    return None


def _all_dataset_urns(gms: Gms) -> list[str]:
    # One query per platform: an unfiltered search collapses the dbt and Postgres siblings and
    # returns one of each pair, which would miss half the datasets when filing them.
    return gms.search("dataset", platform="postgres") + gms.search("dataset", platform="dbt")


def _schema_from_urn(urn: str) -> str | None:
    """``urn:li:dataset:(urn:li:dataPlatform:<p>,<db>.<schema>.<table>,PROD)`` -> ``<schema>``."""
    try:
        return urn.split(",")[1].split(".")[1]
    except IndexError:
        return None


def _governed_tag(tag: str, value: str) -> str:
    return make_tag_urn(f"{tag}.{value}")


def _concept_term(concept_id: str) -> str:
    return make_term_urn(f"Ontology.{concept_id}")


def _identified_entities() -> dict[str, str]:
    """Value type -> the entity type it identifies (it plays a role in an ``identify_by``)."""
    defined = registry.concepts()
    out: dict[str, str] = {}
    for entity in registry.entity_types():
        for concept in registry.identifying_concepts(entity):
            if concept in defined:
                out.setdefault(concept, entity["concept"])
    return out


def _ontology_aspects() -> list[MetadataChangeProposalWrapper]:
    """The business glossary and the data dictionary (ADR-0027).

    * Business glossary (glossary terms, grouped by the responsible domain, which owns each term):
      every entity type, and every value type the ontology gives a business definition. A term
      shows the readable name of its concept (``CustomerKey`` -> "Customer Key"), and keeps the
      concept itself as a property.
    * Data dictionary (the ``Data element`` structured property): its allowed values are the data
      elements, each with its definition. Every linked column carries the one it is. A data element
      is not a business term, so it is not a glossary term.
    """
    defined = {c["concept"]: c for c in registry.ontology().get("ontology") or []}
    meaning = registry.concepts()
    value_owner, entity_of = registry.concept_owners(), registry.concept_entities()
    entities = {e["concept"]: e for e in registry.entity_types()}
    titles = {d["name"]: d.get("display_name", d["name"]) for d in registry.domains()}
    business_values = {v for v in meaning if defined.get(v, {}).get("description")}
    mcps = [
        MetadataChangeProposalWrapper(
            entityUrn=BUSINESS_NODE,
            aspect=GlossaryNodeInfoClass(
                name="Business glossary",
                definition="Business terms: what the business means, the rule behind it, what it "
                "is used for, and the domain responsible for it.",
            ),
        )
    ]
    for domain in sorted(
        {e["owner"] for e in entities.values()} | {value_owner[v] for v in business_values}
    ):
        title = titles.get(domain, domain)
        mcps.append(
            MetadataChangeProposalWrapper(
                entityUrn=f"{BUSINESS_NODE}.{domain}",
                aspect=GlossaryNodeInfoClass(
                    name=title,
                    definition=f"Business terms {title} is responsible for.",
                    parentNode=BUSINESS_NODE,
                ),
            )
        )

    def term(concept: str, name: str, definition: str, owner: str, related: list[str]) -> None:
        urn = _concept_term(concept)
        info = GlossaryTermInfoClass(
            name=name,
            definition=definition,
            parentNode=f"{BUSINESS_NODE}.{owner}",
            termSource="INTERNAL",
            customProperties={"concept": concept, "responsible_domain": owner},
        )
        owners = OwnershipClass(
            owners=[OwnerClass(owner=make_group_urn(owner), type=OwnershipTypeClass.BUSINESS_OWNER)]
        )
        mcps.append(MetadataChangeProposalWrapper(entityUrn=urn, aspect=info))
        mcps.append(MetadataChangeProposalWrapper(entityUrn=urn, aspect=owners))
        if related:
            links = GlossaryRelatedTermsClass(relatedTerms=[_concept_term(r) for r in related])
            mcps.append(MetadataChangeProposalWrapper(entityUrn=urn, aspect=links))

    for name, e in entities.items():
        related = [v for v, owner in entity_of.items() if owner == name and v in business_values]
        term(
            name,
            registry.display_name(name),
            " ".join(e["description"].split()),
            e["owner"],
            related,
        )
    for value in sorted(business_values):
        definition = " ".join(str(defined[value]["description"]).split())
        term(
            value,
            registry.display_name(value),
            definition,
            value_owner[value],
            [entity_of[value]] if value in entity_of else [],
        )

    mcps.append(
        MetadataChangeProposalWrapper(
            entityUrn=DATA_ELEMENT,
            aspect=StructuredPropertyDefinitionClass(
                qualifiedName="dgp.data_element",
                displayName="Data element",
                description="The data element a column holds: its definition in the data "
                "dictionary (domains/<domain>/contracts/dictionary/), linked from the column's "
                "contract and inherited by it (ADR-0027).",
                valueType="urn:li:dataType:datahub.string",
                cardinality=PropertyCardinalityClass.SINGLE,
                entityTypes=["urn:li:entityType:datahub.schemaField"],
                allowedValues=[
                    PropertyValueClass(
                        value=name,
                        description=f"{registry.display_name(name)} ({value_owner[name]}): "
                        + " ".join(str(element.get("description", "")).split()),
                    )
                    for name, element in sorted(meaning.items())
                ],
            ),
        )
    )
    return mcps


def _column_concepts(columns: list[dict], contract: Path) -> dict[str, str]:
    """Column -> the ontology concept it links, for every linked column of a contract."""
    return {c["name"]: concept for c in columns if (concept := registry.concept_of(c, contract))}


def _business_concepts() -> set[str]:
    """The value types that are business terms (they carry a business ``description``)."""
    return {
        c["concept"]
        for c in registry.ontology().get("ontology") or []
        if c.get("type") == "ValueType" and c.get("description")
    }


def _deprecation(port: products.OutputPort) -> DeprecationClass:
    """The port version's deprecation (ADR-0019 §3): its sunset and successor, or none."""
    actor = "urn:li:corpuser:datahub"
    if port.status != "deprecated":
        return DeprecationClass(deprecated=False, note="", actor=actor)
    successor = next((p.name for p in products.data_products() if p.version_of == port.name), None)
    sunset = port.sunset
    when = (
        int(datetime.fromisoformat(f"{sunset}T00:00:00+00:00").timestamp() * 1000)
        if sunset
        else None
    )
    note = f"Deprecated port version. Migrate to `{successor or 'its successor'}`" + (
        f" before {sunset}." if sunset else "."
    )
    return DeprecationClass(deprecated=True, note=note, actor=actor, decommissionTime=when)


# The publishing steps, in order.


def publish_domains(emitter: DatahubRestEmitter) -> None:
    """One DataHub domain per mesh domain, named and described from the registry."""
    for meta in registry.domains():
        emitter.emit(
            MetadataChangeProposalWrapper(
                entityUrn=make_domain_urn(meta["name"]),
                aspect=DomainPropertiesClass(
                    name=meta.get("display_name", meta["name"]), description=meta.get("description")
                ),
            )
        )
    print(f"  domains: {', '.join(registry.domain_names())}")


def file_datasets(emitter: DatahubRestEmitter, gms: Gms) -> None:
    """File every warehouse and dbt dataset under the domain its schema belongs to."""
    domains, filed = registry.domain_names(), 0
    for urn in _all_dataset_urns(gms):
        schema = _schema_from_urn(urn)
        domain = _domain_of(schema, domains) if schema else None
        if not domain:
            continue
        emitter.emit(
            MetadataChangeProposalWrapper(
                entityUrn=urn, aspect=DomainsClass(domains=[make_domain_urn(domain)])
            )
        )
        filed += 1
    print(f"  datasets filed under a domain: {filed}")


def publish_tags_and_glossary(emitter: DatahubRestEmitter) -> None:
    """The governed tags (sensitivity, privacy category) as DataHub tags (a governance label, like
    the warehouse tag each mirrors), then the business glossary and the data dictionary. Always
    published: a landing file carries them from here whatever the warehouse."""
    governed = registry.tags()
    for name, tag in governed.items():
        for value in tag["values"]:
            emitter.emit(
                MetadataChangeProposalWrapper(
                    entityUrn=_governed_tag(name, value["value"]),
                    aspect=TagPropertiesClass(
                        name=f"{name}: {value['value']}", description=value.get("description")
                    ),
                )
            )
    print(
        f"  governed tags: {', '.join(governed)}"
        + (" (warehouse assets carry the native ones)" if NATIVE_TAGS else "")
    )
    aspects = _ontology_aspects()
    for mcp in aspects:
        emitter.emit(mcp)
    print(f"  business glossary + data dictionary: {len(aspects)} aspects")


def _field_info(
    emitter: DatahubRestEmitter,
    urn: str,
    columns: list[dict],
    contract: Path,
    business: set[str],
    *,
    tagged: bool,
) -> list[EditableSchemaFieldInfoClass]:
    """Each column's meaning (ADR-0027), from its resolved contract: its data element (a
    structured property), the business term that element represents or (for an identifier) the
    entity it identifies, and, when ``tagged`` (the asset's platform has no native tags), its
    governed tags: sensitivity from ``classification``, privacy from ``privacyCategory``."""
    identified = _identified_entities()
    linked = _column_concepts(columns, contract)
    fields = []
    for column in columns:
        col, governed = column["name"], registry.column_tags(column)
        element = linked.get(col)
        terms = [_concept_term(element)] if element in business else []
        if element in identified:
            terms.append(_concept_term(identified[element]))
        if element:
            emitter.emit(
                MetadataChangeProposalWrapper(
                    entityUrn=make_schema_field_urn(urn, col),
                    aspect=StructuredPropertiesClass(
                        properties=[
                            StructuredPropertyValueAssignmentClass(
                                propertyUrn=DATA_ELEMENT, values=[element]
                            )
                        ]
                    ),
                )
            )
        tags = [
            TagAssociationClass(tag=_governed_tag(name, value))
            for name, value in (governed if tagged else {}).items()
        ]
        fields.append(
            EditableSchemaFieldInfoClass(
                fieldPath=col,
                globalTags=GlobalTagsClass(tags=tags),
                glossaryTerms=GlossaryTermsClass(
                    terms=[GlossaryTermAssociationClass(urn=t) for t in terms], auditStamp=AUDIT
                ),
            )
        )
    return fields


def _publish_meaning(
    emitter: DatahubRestEmitter,
    urn: str,
    schema: dict,
    contract: Path,
    business: set[str],
    *,
    tagged: bool,
) -> None:
    """A contracted table's meaning on one asset: the term of the entity one row is on the table,
    and each column's data element, business term and (when ``tagged``) governed tags."""
    columns = [c for c in schema.get("properties") or [] if c.get("name")]
    fields = _field_info(emitter, urn, columns, contract, business, tagged=tagged)
    entity = registry.entity_of_table(schema)
    emitter.emit(
        MetadataChangeProposalWrapper(
            entityUrn=urn,
            aspect=GlossaryTermsClass(
                terms=[GlossaryTermAssociationClass(urn=_concept_term(entity))] if entity else [],
                auditStamp=AUDIT,
            ),
        )
    )
    if fields:
        emitter.emit(
            MetadataChangeProposalWrapper(
                entityUrn=urn,
                aspect=EditableSchemaMetadataClass(
                    editableSchemaFieldInfo=fields, created=AUDIT, lastModified=AUDIT
                ),
            )
        )


def publish_ingested(emitter: DatahubRestEmitter) -> None:
    """The meaning of every dataset the platform ingests, on each asset it is.

    A dataset's resolved source contract (landing or OLTP) goes on the raw table it loads into, and
    a landing one on its landing file too (S3, always tagged here: object storage has no column
    tags). Every raw table, an OLTP source's too, also carries the platform's system columns
    (ADR-0028). A raw table is tagged here unless the warehouse has native tags (the loader sets
    those). Its descriptions are the loader's COMMENTs, which the warehouse ingest reads. A raw
    table that is a dbt source has a dbt sibling, but the warehouse entity is the primary one (the
    dbt recipe's dbt_is_primary_sibling), so nothing is written to the dbt entity."""
    business, n = _business_concepts(), 0
    system = [dict(c) for c in registry.system_columns()]
    for d in load_datasets(root()):
        path = contract_path(root(), d.domain, d.name)
        schema = (odcs.load(path).get("schema") or [{}])[0] if path.exists() else {}
        if d.source_type == "landing" and path.exists():
            s3_urn = make_dataset_urn("s3", layout.catalog_name(ARCHIVE, d.source_system, d.name))
            _publish_meaning(emitter, s3_urn, schema, path, business, tagged=True)
        raw = {**schema, "properties": [*(schema.get("properties") or []), *system]}
        raw_urn = make_dataset_urn(WAREHOUSE_PLATFORM, f"{DB}.{d.raw_schema}.{d.raw_table}")
        _publish_meaning(emitter, raw_urn, raw, path, business, tagged=not NATIVE_TAGS)
        n += 1
    print(f"  ingested datasets with their meaning (landing file, raw table): {n}")


def publish_products(emitter: DatahubRestEmitter) -> None:
    """Every data product, with its output port's meaning, owner, domain and deprecation."""
    business = _business_concepts()
    for product in products.data_products():
        owner = product.owner
        if not owner:
            continue
        location = products.location(owner, product.name)
        if location is None:
            continue
        schema, table = location
        pg_urn = make_dataset_urn("postgres", f"{DB}.{schema}.{table}", "PROD")
        port = products.output_port(product.name, owner)
        if port is not None:
            _publish_meaning(
                emitter, pg_urn, port.schema, port.contract, business, tagged=not NATIVE_TAGS
            )
        assets = [pg_urn, make_dataset_urn("dbt", f"{DB}.{schema}.{table}", "PROD")]
        dp = DataProduct(
            id=f"{owner}.{product.name}",
            display_name=product.definition.get("display_name", product.name),
            domain=make_domain_urn(owner),
            description=product.definition.get("purpose"),
            assets=assets,
            owners=[{"id": make_group_urn(owner), "type": "BUSINESS_OWNER"}],
        )
        for mcp in dp.generate_mcp(upsert=True):
            emitter.emit(mcp)
        # Put the product in its domain explicitly (the high-level domain= does not emit it).
        emitter.emit(
            MetadataChangeProposalWrapper(
                entityUrn=f"urn:li:dataProduct:{owner}.{product.name}",
                aspect=DomainsClass(domains=[make_domain_urn(owner)]),
            )
        )
        print(f"  data product: {owner}.{product.name} -> {schema}.{table}")
        if port is not None:
            deprecation = _deprecation(port)
            for urn in assets:
                emitter.emit(MetadataChangeProposalWrapper(entityUrn=urn, aspect=deprecation))


def link_building_dags(emitter: DatahubRestEmitter, gms: Gms) -> None:
    """Link each warehouse dataset to the Airflow DAG that builds it (ADR-0018).

    It is node metadata, not a lineage edge: a DataJob and a dbt model can't merge into one node,
    so an edge would add a second upstream, while a link keeps "which DAG builds this" on the node
    itself. On the dbt node when there is one (the DAG runs dbt, and the Postgres sibling's
    documentation stays the contract's description). On the Postgres node only for tables dbt
    doesn't know (raw tables of a domain with no dbt project yet, loaded by its ingestion DAG).
    """
    domains, linked = registry.domain_names(), 0
    for dataset_name in sorted({u.split(",")[1] for u in _all_dataset_urns(gms)}):
        if not dataset_name.startswith(f"{DB}."):
            continue  # warehouse datasets only (not the shop_oltp or landing sources)
        schema = dataset_name.split(".")[1]
        domain = _domain_of(schema, domains)
        if not domain:
            continue
        urn = make_dataset_urn("dbt", dataset_name, "PROD")
        if not gms.has_aspect(urn, "schemaMetadata"):  # referenced, but never ingested
            urn = make_dataset_urn(WAREHOUSE_PLATFORM, dataset_name, "PROD")
        layer = schema[len(domain) + 1 :]  # raw | bronze | silver | gold
        dag = f"{domain}_ingestion" if layer == "raw" else f"{domain}_transform"
        emitter.emit(
            MetadataChangeProposalWrapper(
                entityUrn=urn,
                aspect=InstitutionalMemoryClass(
                    elements=[
                        InstitutionalMemoryMetadataClass(
                            url=f"{AIRFLOW_URL}/dags/{dag}",
                            description=f"Built by Airflow DAG {dag}",
                            createStamp=AUDIT,
                        )
                    ]
                ),
            )
        )
        linked += 1
    print(f"  datasets linked to their building DAG: {linked}")


def link_bi_datasets(emitter: DatahubRestEmitter) -> None:
    """BI lineage: each Superset dataset (generated from an Ossie semantic model) reads the
    warehouse output port of the same name, so lineage reaches the chart (output port -> BI
    dataset -> chart, proof P7). The URNs match exactly."""
    linked = 0
    for path in ossie.models():
        for dataset in ossie.datasets(ossie.load(path)):
            name = str(dataset.get("source", ""))  # <db>.<schema>.<table>, the same on both sides
            if name.count(".") != 2:
                continue
            upstream = UpstreamClass(
                dataset=make_dataset_urn(WAREHOUSE_PLATFORM, name, "PROD"),
                type=DatasetLineageTypeClass.COPY,
            )
            emitter.emit(
                MetadataChangeProposalWrapper(
                    entityUrn=make_dataset_urn("superset", name, "PROD"),
                    aspect=UpstreamLineageClass(upstreams=[upstream]),
                )
            )
            linked += 1
    print(f"  BI datasets linked to their warehouse port: {linked}")


def main() -> int:
    emitter, gms = DatahubRestEmitter(gms_server=default_url()), Gms()
    publish_domains(emitter)
    file_datasets(emitter, gms)
    publish_tags_and_glossary(emitter)
    publish_products(emitter)
    publish_ingested(emitter)
    link_building_dags(emitter, gms)
    link_bi_datasets(emitter)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
