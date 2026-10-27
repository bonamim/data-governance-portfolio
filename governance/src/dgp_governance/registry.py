"""The mesh's registry: its domains, governed tags and business ontology.

The domains and their owners live in ``governance/domains/``, the governed tags in
``governance/tags/`` and the ontology (entity types, value types and their relationships) in
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
