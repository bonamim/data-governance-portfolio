#!/usr/bin/env python3
"""Push the *governance* layer into DataHub from our source-of-truth definitions.

Technical metadata (datasets, lineage, tests) comes from the dbt/Postgres ingestion recipes.
This adds the mesh governance on top, generated — not hand-maintained in the UI — from the same
definitions that drive grants and the data contracts:

  * **Domains** — one per mesh domain; every warehouse/dbt dataset is filed under its domain.
  * **Data products** — one per ``domains/*/data_products/*.yaml``: display name, purpose,
    owner, and the output-port datasets (Postgres + dbt sibling) as its assets.

Run via ``make catalog-govern`` (isolated DataHub SDK env). Idempotent: re-running upserts.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import yaml
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

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "platform" / "quality" / "src"))
from dgp_platform_quality import odcs  # noqa: E402  (the one contract reader: links inlined)

from governance.registry import (  # noqa: E402
    column_tags,
    concept_entities,
    concept_of,
    concept_owners,
    concepts,
    domains,
    entity_of_table,
    entity_types,
    ontology,
    tags,
)

GMS = os.environ.get("DATAHUB_GMS", "http://localhost:8080")
DB = os.environ.get("ANALYTICS_DB", "analytics")
# The warehouse's DataHub platform — postgres locally, snowflake/bigquery/etc. in the cloud.
WAREHOUSE_PLATFORM = os.environ.get("WAREHOUSE_PLATFORM", "postgres")
AIRFLOW_URL = os.environ.get(
    "AIRFLOW_BASE_URL", f"http://localhost:{os.environ.get('AIRFLOW_API_PORT', '8080')}"
)
# Single source of truth: the domain registry (governance/domains/), the ontology
# (governance/ontology/) and the governed tags (governance/tags/). Nothing here is hardcoded.
REGISTRY = {d["name"]: d for d in domains()}
DOMAINS = list(REGISTRY)
# Governed tags (governance/tags/): name -> its definition, warehouse tag and allowed values.
GOVERNED_TAGS = tags()


def _domain_of(schema: str) -> str | None:
    """Map a warehouse schema (<domain>_<layer>) to its mesh domain (longest match wins)."""
    for d in sorted(DOMAINS, key=len, reverse=True):
        if schema == d or schema.startswith(d + "_"):
            return d
    return None


def _search_datasets(platform: str) -> list[str]:
    body = json.dumps(
        {
            "input": "*",
            "entity": "dataset",
            "start": 0,
            "count": 500,
            "filter": {
                "or": [{"and": [{"field": "platform", "value": f"urn:li:dataPlatform:{platform}"}]}]
            },
        }
    ).encode()
    req = urllib.request.Request(
        f"{GMS}/entities?action=search", data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req) as resp:  # noqa: S310 (local GMS)
        return [e["entity"] for e in json.load(resp)["value"]["entities"]]


def _is_ingested(urn: str) -> bool:
    """True when the entity was ingested (has a schema), not just referenced by a written aspect."""
    url = f"{GMS}/aspects/{urllib.parse.quote(urn, safe='')}?aspect=schemaMetadata&version=0"
    try:
        with urllib.request.urlopen(url):  # noqa: S310 (local GMS)
            return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise


def _all_dataset_urns() -> list[str]:
    # Query per platform: an unfiltered search collapses dbt/Postgres siblings and returns only
    # one of each pair, so we would miss half the datasets for domain filing.
    return _search_datasets("postgres") + _search_datasets("dbt")


def _schema_from_urn(urn: str) -> str | None:
    # urn:li:dataset:(urn:li:dataPlatform:<p>,<db>.<schema>.<table>,PROD)
    try:
        name = urn.split(",")[1]  # <db>.<schema>.<table>
        return name.split(".")[1]
    except IndexError:
        return None


def _port_location(owner: str, product: str) -> tuple[str, str] | None:
    """Physical (schema, table) of an output port, from its dbt model's layer folder."""
    for path in (REPO_ROOT / "domains" / owner / "dbt" / "models").rglob(f"{product}.sql"):
        return f"{owner}_{path.parent.name}", product
    return None


BUSINESS_NODE = "urn:li:glossaryNode:BusinessGlossary"
DATA_ELEMENT = "urn:li:structuredProperty:dgp.data_element"
# A warehouse with native tags carries the governed tags itself (the dbt build sets them) and
# DataHub ingests it from there; only a warehouse without tags (Postgres) gets it from here.
NATIVE_TAGS = WAREHOUSE_PLATFORM in {"snowflake", "databricks", "bigquery"}


def _governed_tag(tag: str, value: str) -> str:
    return make_tag_urn(f"{tag}.{value}")


def _concept_term(concept_id: str) -> str:
    return make_term_urn(f"Ontology.{concept_id}")


def _identified_entities() -> dict[str, str]:
    """Value type -> the entity type it identifies (it plays a role in an ``identify_by``)."""
    out: dict[str, str] = {}
    defined = concepts()
    for e in entity_types():
        rels = {r["name"]: r for r in e.get("relationships") or []}
        for ident in e.get("identify_by") or []:
            for role in rels.get(ident, {}).get("roles") or []:
                if role["concept"] in defined:
                    out.setdefault(role["concept"], e["concept"])
    return out


def _ontology_aspects() -> list[MetadataChangeProposalWrapper]:
    """The business glossary and the data dictionary (ADR-0027).

    * Business glossary (glossary terms, grouped by the responsible domain, which owns each term):
      every entity type, and every value type the ontology gives a business definition.
    * Data dictionary (the ``Data element`` structured property): its allowed values are the data
      elements, each with its definition; every linked column carries the one it is. A data element
      is not a business term, so it is not a glossary term.
    """
    defined = {c["concept"]: c for c in ontology().get("ontology") or []}
    meaning, value_owner, entity_of = concepts(), concept_owners(), concept_entities()
    entities = {e["concept"]: e for e in entity_types()}
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
        title = REGISTRY.get(domain, {}).get("display_name", domain)
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
        term(name, name, " ".join(e["description"].split()), e["owner"], related)
    for value in sorted(business_values):
        title = meaning[value].get("businessName", value)
        definition = " ".join(str(defined[value]["description"]).split())
        term(
            value,
            title,
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
                description="The data element a column holds — its definition in the data "
                "dictionary (domains/<domain>/contracts/definitions/), linked from the column's "
                "contract and inherited by it (ADR-0027).",
                valueType="urn:li:dataType:datahub.string",
                cardinality=PropertyCardinalityClass.SINGLE,
                entityTypes=["urn:li:entityType:datahub.schemaField"],
                allowedValues=[
                    PropertyValueClass(
                        value=name,
                        description=f"{element.get('businessName', name)} ({value_owner[name]}): "
                        + " ".join(str(element.get("description", "")).split()),
                    )
                    for name, element in sorted(meaning.items())
                ],
            ),
        )
    )
    return mcps


def _column_concepts(contract_path: Path) -> dict[str, str]:
    """{column: concept id} for every column of a contract that links an ontology concept."""
    defn = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
    return {
        prop["name"]: concept
        for obj in defn.get("schema") or []
        for prop in obj.get("properties") or []
        if prop.get("name") and (concept := concept_of(prop, contract_path))
    }


def _columns_with_tags(contract_path: Path) -> list[tuple[str, dict[str, str]]]:
    """(column, {governed tag: value}) for every column of a resolved ODCS output-port contract —
    sensitivity from ``classification``, privacy category from ``privacyCategory``."""
    defn = odcs.load(contract_path)
    return [
        (prop["name"], column_tags(prop))
        for obj in defn.get("schema") or []
        for prop in obj.get("properties") or []
        if prop.get("name")
    ]


def _deprecation(contract: Path, port: str) -> DeprecationClass:
    """The port version's deprecation (ADR-0019 §3): sunset + successor, or not deprecated."""
    doc = odcs.load(contract)
    actor = "urn:li:corpuser:datahub"
    if doc.get("status") != "deprecated":
        return DeprecationClass(deprecated=False, note="", actor=actor)
    props = {p.get("property"): p.get("value") for p in doc.get("customProperties") or []}
    sunset = props.get("sunsetDate")
    successor = next(
        (
            p.stem
            for p in REPO_ROOT.glob("domains/*/data_products/*.yaml")
            if (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("version_of") == port
        ),
        None,
    )
    when = (
        int(datetime.fromisoformat(f"{sunset}T00:00:00+00:00").timestamp() * 1000)
        if sunset
        else None
    )
    note = f"Deprecated port version. Migrate to `{successor or 'its successor'}`" + (
        f" before {sunset}." if sunset else "."
    )
    return DeprecationClass(deprecated=True, note=note, actor=actor, decommissionTime=when)


def main() -> int:
    emitter = DatahubRestEmitter(gms_server=GMS)

    # 1. Domains (name + description from the registry)
    for d in DOMAINS:
        meta = REGISTRY[d]
        emitter.emit(
            MetadataChangeProposalWrapper(
                entityUrn=make_domain_urn(d),
                aspect=DomainPropertiesClass(
                    name=meta.get("display_name", d), description=meta.get("description")
                ),
            )
        )
    print(f"  domains: {', '.join(DOMAINS)}")

    # 2. File every dataset under its domain
    filed = 0
    for urn in _all_dataset_urns():
        schema = _schema_from_urn(urn)
        domain = _domain_of(schema) if schema else None
        if not domain:
            continue
        emitter.emit(
            MetadataChangeProposalWrapper(
                entityUrn=urn, aspect=DomainsClass(domains=[make_domain_urn(domain)])
            )
        )
        filed += 1
    print(f"  datasets filed under a domain: {filed}")

    # 3. The governed tags (governance/tags/: sensitivity, privacy category) as DataHub tags — a
    #    governance label, like the warehouse tag each mirrors — and the business glossary.
    for name, tag in {} if NATIVE_TAGS else GOVERNED_TAGS.items():
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
        "  governed tags: from the warehouse (native tags)"
        if NATIVE_TAGS
        else f"  governed tags: {', '.join(GOVERNED_TAGS)}"
    )
    ontology_mcps = _ontology_aspects()
    for mcp in ontology_mcps:
        emitter.emit(mcp)
    print(f"  business glossary + data dictionary: {len(ontology_mcps)} aspects")

    # 4. Data products + per-column meaning and governed tags on their output ports
    audit = AuditStampClass(time=0, actor="urn:li:corpuser:datahub")
    identified = _identified_entities()
    business = {
        c["concept"]
        for c in ontology().get("ontology") or []
        if c.get("type") == "ValueType" and c.get("description")
    }
    for path in REPO_ROOT.glob("domains/*/data_products/*.yaml"):
        defn = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        owner = (defn.get("owner") or {}).get("team")
        name = defn.get("name")
        loc = _port_location(owner, name) if owner and name else None
        if not loc:
            continue
        schema, table = loc
        pg_urn = make_dataset_urn("postgres", f"{DB}.{schema}.{table}", "PROD")

        # Meaning on the output port (ADR-0027): the table carries the business term of the entity
        # one row is; each column its data element (structured property), the business term its
        # data element represents, or — an identifier — the entity it identifies; and, without
        # native warehouse tags, its governed tags (sensitivity, privacy category).
        ports = defn.get("output_ports") or []
        contracts = [op.get("contract") for op in ports if op.get("contract")]
        fields = []
        table_terms: list[str] = []
        for contract in contracts:
            linked = _column_concepts(REPO_ROOT / contract)
            doc = yaml.safe_load((REPO_ROOT / contract).read_text(encoding="utf-8")) or {}
            table_terms += [
                _concept_term(e) for o in doc.get("schema") or [] if (e := entity_of_table(o))
            ]
            for col, governed in _columns_with_tags(REPO_ROOT / contract):
                element = linked.get(col)
                terms = [_concept_term(element)] if element in business else []
                if element in identified:
                    terms.append(_concept_term(identified[element]))
                if element:
                    emitter.emit(
                        MetadataChangeProposalWrapper(
                            entityUrn=make_schema_field_urn(pg_urn, col),
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
                    for name, value in ({} if NATIVE_TAGS else governed).items()
                ]
                fields.append(
                    EditableSchemaFieldInfoClass(
                        fieldPath=col,
                        globalTags=GlobalTagsClass(tags=tags),
                        glossaryTerms=GlossaryTermsClass(
                            terms=[GlossaryTermAssociationClass(urn=t) for t in terms],
                            auditStamp=audit,
                        ),
                    )
                )
        emitter.emit(
            MetadataChangeProposalWrapper(
                entityUrn=pg_urn,
                aspect=GlossaryTermsClass(
                    terms=[GlossaryTermAssociationClass(urn=t) for t in table_terms],
                    auditStamp=audit,
                ),
            )
        )
        if fields:
            emitter.emit(
                MetadataChangeProposalWrapper(
                    entityUrn=pg_urn,
                    aspect=EditableSchemaMetadataClass(
                        editableSchemaFieldInfo=fields, created=audit, lastModified=audit
                    ),
                )
            )
        assets = [pg_urn, make_dataset_urn("dbt", f"{DB}.{schema}.{table}", "PROD")]
        dp = DataProduct(
            id=f"{owner}.{name}",
            display_name=defn.get("display_name", name),
            domain=make_domain_urn(owner),
            description=defn.get("purpose"),
            assets=assets,
            owners=[{"id": make_group_urn(owner), "type": "BUSINESS_OWNER"}],
        )
        for mcp in dp.generate_mcp(upsert=True):
            emitter.emit(mcp)
        # Put the product in its domain explicitly (the high-level domain= does not emit it).
        emitter.emit(
            MetadataChangeProposalWrapper(
                entityUrn=f"urn:li:dataProduct:{owner}.{name}",
                aspect=DomainsClass(domains=[make_domain_urn(owner)]),
            )
        )
        print(f"  data product: {owner}.{name} -> {schema}.{table}")
        for contract in contracts:
            if (dep := _deprecation(REPO_ROOT / contract, name)) is not None:
                for urn in assets:
                    emitter.emit(MetadataChangeProposalWrapper(entityUrn=urn, aspect=dep))

    # 5. Link each warehouse dataset to the Airflow DAG that builds it — as node metadata, not a
    #    lineage edge (a DataJob and a dbt model can't merge into one node, so an edge would add a
    #    second upstream; a link keeps the "which DAG builds this" on the node itself). ADR-0018.
    #    On the dbt node when there is one (the DAG runs dbt; the Postgres sibling's Documentation
    #    stays the contract's description); on the Postgres node only for tables dbt doesn't know
    #    (raw tables of domains with no dbt project yet, loaded by their ingestion DAG).
    linked = 0
    for dataset_name in sorted({u.split(",")[1] for u in _all_dataset_urns()}):
        if not dataset_name.startswith(f"{DB}."):
            continue  # warehouse datasets only (skip the shop_oltp / landing sources)
        schema = dataset_name.split(".")[1]
        domain = _domain_of(schema)
        if not domain:
            continue
        urn = make_dataset_urn("dbt", dataset_name, "PROD")
        if not _is_ingested(urn):
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
                            createStamp=audit,
                        )
                    ]
                ),
            )
        )
        linked += 1
    print(f"  datasets linked to their building DAG: {linked}")

    # 6. BI lineage: each Superset dataset (generated from an Ossie semantic model) reads the
    #    warehouse output port of the same name — emit it so lineage reaches the chart
    #    (output port -> BI dataset -> chart, finishing proof P7). URNs match exactly.
    bi = 0
    for sm in REPO_ROOT.glob("domains/*/semantics/*.semantic_model.yaml"):
        spec = yaml.safe_load(sm.read_text(encoding="utf-8")) or {}
        for ds in spec.get("datasets") or []:
            name = str(ds.get("source", ""))  # <db>.<schema>.<table>, shared by both platforms
            if name.count(".") != 2:
                continue
            emitter.emit(
                MetadataChangeProposalWrapper(
                    entityUrn=make_dataset_urn("superset", name, "PROD"),
                    aspect=UpstreamLineageClass(
                        upstreams=[
                            UpstreamClass(
                                dataset=make_dataset_urn(WAREHOUSE_PLATFORM, name, "PROD"),
                                type=DatasetLineageTypeClass.COPY,
                            )
                        ]
                    ),
                )
            )
            bi += 1
    print(f"  BI datasets linked to their warehouse port: {bi}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
