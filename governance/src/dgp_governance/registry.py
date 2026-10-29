"""The mesh's registry: its domains, governed tags and business ontology.

The domains and their owners live in ``governance/domains/``, the governed tags in
``governance/tags/``, the platform's system columns in ``governance/system_columns.yaml``
(ADR-0028) and the ontology (entity types, value types and their relationships) in
``governance/ontology/`` (ADR-0027). The policy checks, the dbt generator, the catalog publisher
and the semantic agents all read them from here, so none of them hardcodes a domain, a tag or a
concept. Those copies used to drift apart.
"""

from __future__ import annotations

import re
from pathlib import Path

from dgp_platform_contracts import odcs

from .repo import domain_of, load_yaml, root


def _governance(base: Path | None = None) -> Path:
    return (base or root()) / "governance"


def domains(base: Path | None = None) -> list[dict]:
    """Every registered domain, as written in its registry file (name, owner, alerts, …)."""
    return [load_yaml(p) for p in sorted((_governance(base) / "domains").glob("*.yaml"))]


def domain_names(base: Path | None = None) -> list[str]:
    return [d["name"] for d in domains(base)]


def domain(name: str, base: Path | None = None) -> dict | None:
    """The registry entry of one domain, or None if it is not registered."""
    return next((d for d in domains(base) if d.get("name") == name), None)


def tags(base: Path | None = None) -> dict[str, dict]:
    """Governed tags (``governance/tags/<tag>.yaml``): tag name -> definition and allowed values."""
    out = {}
    for path in sorted((_governance(base) / "tags").glob("*.yaml")):
        doc = load_yaml(path)
        out[doc["tag"]] = doc
    return out


def classification_values() -> dict[str, dict]:
    """ODCS ``classification`` value -> its entry in the governed tag that reads it
    (``sensitivity``, SNOWFLAKE.TAGS.SENSITIVITY), most sensitive first."""
    (tag,) = [t for t in tags().values() if (t.get("odcs") or {}).get("field") == "classification"]
    return {v["value"]: v for v in tag["values"]}


def column_tags(prop: dict) -> dict[str, str]:
    """Governed tag -> value for a (resolved) contract column. Each tag reads one ODCS field or
    custom property. A value the tag does not allow is left out (the checks report it)."""
    out: dict[str, str] = {}
    custom = odcs.custom_properties(prop)
    for name, tag in tags().items():
        where = tag.get("odcs") or {}
        value = (
            prop.get(where["field"])
            if "field" in where
            else custom.get(where.get("custom_property"))
        )
        if isinstance(value, str) and value in {v["value"] for v in tag["values"]}:
            out[name] = value
    return out


def warehouse_tags(columns: list[dict]) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """The governed tags of a table as the warehouse names them (``{warehouse tag: value}``, e.g.
    ``SNOWFLAKE.TAGS.SENSITIVITY: RESTRICTED``), from its resolved contract columns: each column's
    tags, and the table's sensitivity, the most sensitive of its columns' (privacy category is
    about a column, so a table has none). One answer for every writer: the dbt build of an output
    port and the loader of a raw table."""
    governed = tags()
    warehouse = {
        name: {v["value"]: v["warehouse_value"] for v in tag["values"]}
        for name, tag in governed.items()
    }
    values = {c["name"]: column_tags(c) for c in columns if c.get("name")}
    table: dict[str, str] = {}
    for name, tag in governed.items():
        if (tag.get("odcs") or {}).get("field") != "classification":
            continue
        ranked = [v["value"] for v in tag["values"]]  # most sensitive first
        present = [v[name] for v in values.values() if name in v]
        if present:
            table[tag["warehouse_tag"]] = warehouse[name][min(present, key=ranked.index)]
    per_column = {
        col: {governed[name]["warehouse_tag"]: warehouse[name][value] for name, value in v.items()}
        for col, v in values.items()
    }
    return table, per_column


def _system_registry(base: Path | None = None) -> dict:
    path = _governance(base) / "system_columns.yaml"
    return load_yaml(path) if path.exists() else {}


def system_columns(base: Path | None = None) -> list[dict]:
    """The columns the platform adds to every raw table (``_system_*``, ADR-0028), each an ODCS
    property: name, description, logicalType, classification."""
    return list(_system_registry(base).get("columns") or [])


def reserved_prefixes(base: Path | None = None) -> tuple[str, ...]:
    """Column-name prefixes no source delivers: the platform's and the data products' own."""
    reg = _system_registry(base)
    return tuple(p for p in (reg.get("system_prefix"), reg.get("product_prefix")) if p)


def is_system_column(name: str, base: Path | None = None) -> bool:
    """A column named with the platform's prefix (whether or not the platform defines it)."""
    prefix = _system_registry(base).get("system_prefix")
    return bool(prefix) and name.startswith(prefix)


def with_system_meaning(properties: list[dict], base: Path | None = None) -> list[dict]:
    """Fill each ``_system_*`` property from the platform's definition wherever it leaves a field
    unset, as a linked column inherits its definition: a contract only names it."""
    defined = {c["name"]: c for c in system_columns(base)}
    for prop in properties:
        for key, value in (defined.get(prop.get("name")) or {}).items():
            prop.setdefault(key, value)
    return properties


def ontology_files(base: Path | None = None) -> list[Path]:
    """The ontology documents. There must be exactly one (``check_ontology`` says so)."""
    return sorted((_governance(base) / "ontology").glob("*.ontology.yaml"))


def ontology(base: Path | None = None) -> dict:
    """The business ontology: its one Apache Ossie document."""
    (path,) = ontology_files(base)
    return load_yaml(path)


def ontology_link(doc: dict) -> str | None:
    """The ontology concept an ODCS element names with its ``ontology`` link
    (``<…>.ontology.yaml#<Concept>``): a data element's value type, a table's entity type."""
    for link in doc.get("authoritativeDefinitions") or []:
        url = str(link.get("url", ""))
        if link.get("type") == "ontology" and ".ontology.yaml#" in url:
            return url.rsplit("#", 1)[1]
    return None


def definition_files(base: Path | None = None) -> dict[str | None, list[Path]]:
    """Value type -> its data element file(s) in ``domains/<domain>/contracts/definitions/``,
    keyed by the value type each file names with its ``ontology`` link (None: files that name
    none). A value type has exactly one. ``check_ontology`` reports duplicates and missing links."""
    out: dict[str | None, list[Path]] = {}
    for path in sorted((base or root()).glob("domains/*/contracts/definitions/*.odcs.yaml")):
        out.setdefault(ontology_link(load_yaml(path)), []).append(path)
    return out


def display_name(concept: str) -> str:
    """The readable name of an ontology concept, derived from its PascalCase name (``CustomerKey``
    -> ``Customer Key``). Ossie has no label field, so the name is the one source: a catalog shows
    this, a technical tool the concept itself, and nothing stores a second label (ADR-0027)."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", concept)


def concepts() -> dict[str, dict]:
    """Value type -> its data element (the ODCS property a linked column inherits)."""
    return {name: load_yaml(paths[0]) for name, paths in definition_files().items() if name}


def concept_owners() -> dict[str, str]:
    """Value type -> the domain that defines it (where its definition file lives)."""
    return {name: domain_of(paths[0]) for name, paths in definition_files().items() if name}


def concept_entities() -> dict[str, str]:
    """Value type -> the entity type that declares a relationship to it first."""
    out: dict[str, str] = {}
    for concept in ontology().get("ontology") or []:
        if concept.get("type") == "EntityType":
            for rel in concept.get("relationships") or []:
                for role in rel.get("roles") or []:
                    out.setdefault(role["concept"], concept["concept"])
    return out


def identifying_concepts(entity: dict) -> list[str]:
    """The value types that identify an entity type (the roles of its ``identify_by``)."""
    relationships = {r["name"]: r for r in entity.get("relationships") or []}
    return [
        role["concept"]
        for name in entity.get("identify_by") or []
        for role in relationships.get(name, {}).get("roles") or []
    ]


def entity_types() -> list[dict]:
    """Every entity type, with its ``owner``: the domain defining its identifying value types."""
    owners = concept_owners()
    out = []
    for concept in ontology().get("ontology") or []:
        if concept.get("type") != "EntityType":
            continue
        defined_by = sorted({owners[v] for v in identifying_concepts(concept) if v in owners})
        out.append({**concept, "owner": defined_by[0] if len(defined_by) == 1 else None})
    return out


def concept_of(prop: dict, contract: Path) -> str | None:
    """The value type a contract column holds: follow its ``businessDefinition`` link (relative to
    ``contract``) to the data element, and read the value type the data element names."""
    for link in prop.get("authoritativeDefinitions") or []:
        url = str(link.get("url", ""))
        if link.get("type") != "businessDefinition" or not url.endswith(".odcs.yaml"):
            continue
        target = (Path(contract).parent / url).resolve()
        if "definitions" in target.parts and target.is_file():
            return ontology_link(load_yaml(target))
    return None


def entity_of_table(obj: dict) -> str | None:
    """The entity type a contract's schema object (a table or file) represents (one row is one of
    it), from its ODCS ``ontology`` link, or None."""
    return ontology_link(obj)
