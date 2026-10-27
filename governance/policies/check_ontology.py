#!/usr/bin/env python3
"""Federated governance policy: the business ontology, and every contract's links to it (ADR-0027).

The ontology's structure is one Apache Ossie document (``governance/ontology/*.ontology.yaml``).
Each value type's meaning is an ODCS property that the domain owning it defines in
``domains/<domain>/contracts/definitions/<value_type>.odcs.yaml`` (shared places in the
reference domain), and every contract column that means it links there and inherits it.

  Ontology
    1. There is one ontology document, and Ossie's own validator passes it (vendored, unchanged:
       ``dgp_platform_semantics/vendor/ossie/``): schema, unique names, references. Concept
       names are PascalCase (``CustomerKey``, ``OrderId``) and relationship names snake_case
       (``placed_by``), as Ossie's examples write them: a name is an identifier, referenced from
       ``verbalizes``, the mappings, ODCS links and catalog URNs, so it never holds a space.
    2. Value types and data element files match one to one: each file names the value type it holds
       with an ODCS ``ontology`` link (``<…>.ontology.yaml#<ValueType>``) that resolves. No value
       type is defined twice. Governed-tag values (``privacyCategory``) are allowed values.
    3. Every entity type is a business term with a business definition (its description). A value
       type with a description is a business term too, and one without is a data element only.
    4. Ownership comes from where things live, not from a list: a value type belongs to the domain
       whose folder defines it, an entity type to the domain that defines its identifying value
       types. That domain is registered, and it also defines every value type the entity type
       declares first.
    5. A definition is a valid ODCS property carrying meaning only (description, business name, a
       taxonomy classification, and a logicalType that agrees with what its value type extends),
       and nothing table-specific (physical type, keys, required, quality…).
    6. Every definition is used by a contract column.

  Contracts (output ports and landing)
    7. A column takes its meaning only from a definition: any link datacontract-cli resolves
       (``businessDefinition``, the ODCS type, or ``semantics``/``definition``) points at a
       definition file, and resolves.
    8. One place per fact: a linked column does not restate what its definition says, or blank it.
    9. A linked column's logicalType does not contradict its definition's.
    10. A column name used in two or more contracts links a definition in every one of them. Two
       definitions under one name is a homonym, and it is explicit: each column links its own.
    11. A table that declares the entity it represents (an ODCS ``ontology`` link on the schema
       object, ``…commerce.ontology.yaml#Order``) names an entity type of the ontology, in a file
       that exists.

A column may still set its own description or classification where the context changes it (a
customer's zip-code prefix is internal, the definition's public), and the contract's value wins.
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path

from _checks import report
from dgp_governance import checks, registry
from dgp_governance.repo import domain_of, load_yaml, relative, root
from dgp_platform_contracts import odcs
from dgp_platform_semantics import ossie
from open_data_contract_standard.model import SchemaProperty
from pydantic import ValidationError

LOGICAL_TYPES = {
    "String": "string",
    "Integer": "integer",
    "Date": "date",
    "DateTime": "timestamp",
    "Boolean": "boolean",
    "Decimal": "number",
    "Float": "number",
}
TABLE_SPECIFIC = {
    "id",
    "name",
    "physicalType",
    "physicalName",
    "primaryKey",
    "primaryKeyPosition",
    "required",
    "unique",
    "partitioned",
    "partitionKeyPosition",
    "encryptedName",
    "transformSourceObjects",
    "transformLogic",
    "transformDescription",
    "relationships",
    "quality",
    "properties",
    "items",
    "map",
}
NOT_RESTATED = [
    "description",
    "businessName",
    "classification",
    "logicalTypeOptions",
    "synonyms",
    "examples",
]


def _contracts(base: Path) -> list[Path]:
    """Every contract (output ports and landing), but not the definitions."""
    paths = base.glob("domains/*/contracts/**/*.odcs.yaml")
    return sorted(p for p in paths if "definitions" not in p.parts)


def _properties(doc: dict) -> Iterator[dict]:
    for obj in doc.get("schema") or []:
        yield from obj.get("properties") or []


def _tag_value_errors(prop: dict) -> list[str]:
    """A governed tag read from an ODCS custom property, with a value the tag does not allow."""
    custom = odcs.custom_properties(prop)
    out = []
    for name, tag in registry.tags().items():
        key = (tag.get("odcs") or {}).get("custom_property")
        allowed = [v["value"] for v in tag["values"]]
        if key in custom and custom[key] not in allowed:
            out.append(f"{key} '{custom[key]}' is not a value of tag '{name}' ({allowed})")
    return out


def _root_type(name: str, defined: dict[str, dict]) -> str | None:
    """The built-in a value type extends, directly or through other value types."""
    seen = set()
    while name in defined and name not in seen:
        seen.add(name)
        parents = defined[name].get("extends") or []
        name = parents[0] if parents else None
    return name if name in LOGICAL_TYPES else None


def _link_resolves(path: Path, link: dict) -> bool:
    return (path.parent / str(link.get("url", "")).partition("#")[0]).resolve().is_file()


# The ontology (rules 1-6).


def _definition_file_violations(definitions: dict[str | None, list[Path]], base: Path) -> list[str]:
    """Rule 2: every data element file names its value type with a link that resolves, and uses
    allowed governed-tag values."""
    out = [
        f"{relative(path, base)}: a data element must name the value type it holds "
        "(authoritativeDefinitions: [{type: ontology, url: <…>.ontology.yaml#<Type>}])"
        for path in definitions.get(None, [])
    ]
    for name, paths in definitions.items():
        for path in paths if name else []:
            doc = load_yaml(path)
            for link in doc.get("authoritativeDefinitions") or []:
                if link.get("type") == "ontology" and not _link_resolves(path, link):
                    out.append(
                        f"{relative(path, base)}: ontology link '{link.get('url')}' does not "
                        "resolve"
                    )
            out += [f"{relative(path, base)}: {v}" for v in _tag_value_errors(doc)]
    return out


PASCAL_CASE = re.compile(r"[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)*")
SNAKE_CASE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")


def _naming_violations(defined: dict[str, dict]) -> list[str]:
    """Rule 1: concept names are PascalCase and relationship names snake_case."""
    out = [
        f"concept '{name}': name it in PascalCase, e.g. CustomerKey or OrderId (no spaces, "
        "underscores or all-caps acronyms)"
        for name in sorted(n for n in defined if n and not PASCAL_CASE.fullmatch(n))
    ]
    for name, concept in sorted(defined.items(), key=lambda item: str(item[0])):
        out += [
            f"concept '{name}': relationship '{r.get('name')}': name it in snake_case, "
            "e.g. placed_by"
            for r in concept.get("relationships") or []
            if not SNAKE_CASE.fullmatch(str(r.get("name") or ""))
        ]
    return out


def _term_violations(
    defined: dict[str, dict], definitions: dict[str, list[Path]], base: Path
) -> list[str]:
    """Rules 2-3: entity types are business terms, and value types and definitions match one to
    one."""
    values = {n for n, c in defined.items() if c.get("type") == "ValueType"}
    out = [
        f"entity type '{name}': needs a business definition (description)"
        for name, c in sorted(defined.items())
        if c.get("type") == "EntityType" and not str(c.get("description") or "").strip()
    ]
    for name, paths in sorted(definitions.items()):
        if len(paths) > 1:
            where = ", ".join(relative(p, base) for p in paths)
            out.append(f"value type '{name}' is defined {len(paths)} times ({where}). Define once")
    out += [
        f"value type '{name}': no domain defines it (contracts/definitions/)"
        for name in sorted(values - set(definitions))
    ]
    out += [
        f"definition '{name}': no value type of that name in the ontology"
        for name in sorted(set(definitions) - values)
    ]
    return out


def _ownership_violations(
    defined: dict[str, dict], definitions: dict[str, list[Path]], domains: set[str], base: Path
) -> list[str]:
    """Rule 4: an entity type belongs to the one registered domain defining its identifying value
    types, and that domain defines every value type the entity type declares first."""
    values = {n for n, c in defined.items() if c.get("type") == "ValueType"}
    owner_of = {n: domain_of(p[0], base) for n, p in definitions.items()}
    first_declared: dict[str, str] = {}
    out: list[str] = []
    for name, entity in defined.items():
        if entity.get("type") != "EntityType":
            continue
        identifying = set(registry.identifying_concepts(entity)) & values
        owners = {owner_of[v] for v in identifying if v in owner_of}
        if len(owners) != 1 or not owners <= domains:
            out.append(
                f"entity type '{name}': its identifying value types must be defined by one "
                f"registered domain (found {sorted(owners) or 'none'})"
            )
        for rel in entity.get("relationships") or []:
            for role in rel.get("roles") or []:
                v = role.get("concept")
                if v not in values or first_declared.setdefault(v, name) != name:
                    continue
                if len(owners) == 1 and owner_of.get(v) not in (None, *owners):
                    (owner,) = owners
                    out.append(
                        f"value type '{v}' belongs to entity type '{name}' (domain {owner}) but "
                        f"is defined by {owner_of[v]}. Move its definition to {owner}"
                    )
    out += [
        f"value type '{name}': no entity type declares a relationship to it"
        for name in sorted(values - set(first_declared))
    ]
    return out


def _definition_content_violations(
    name: str, prop: dict, defined: dict[str, dict], classifications: set[str]
) -> list[str]:
    """Rule 5: a definition is a valid ODCS property that carries meaning only."""
    out = []
    try:
        SchemaProperty.model_validate(prop)
    except ValidationError as e:
        out.append(f"definition '{name}': not a valid ODCS property: {e}")
    if unknown := set(prop) - set(SchemaProperty.model_fields):
        out.append(f"definition '{name}': unknown ODCS property fields {sorted(unknown)}")
    if specific := set(prop) & TABLE_SPECIFIC:
        out.append(
            f"definition '{name}': {sorted(specific)} are table-specific. Keep them on the "
            "contract column"
        )
    for key in ("description", "businessName"):
        if not str(prop.get(key) or "").strip():
            out.append(f"definition '{name}': needs a {key}")
    if prop.get("classification") not in classifications:
        out.append(f"definition '{name}': classification must be one of {sorted(classifications)}")
    values = {n for n, c in defined.items() if c.get("type") == "ValueType"}
    extends = _root_type(name, defined)
    expected = LOGICAL_TYPES.get(extends or "")
    if name in values and prop.get("logicalType") != expected:
        out.append(
            f"definition '{name}': logicalType {prop.get('logicalType')} disagrees with the "
            f"ontology (extends {extends}, so {expected})"
        )
    return out


def ontology_violations(
    onto: dict,
    definitions: dict[str | None, list[Path]],
    domains: set[str],
    classifications: set[str],
    base: Path | None = None,
) -> list[str]:
    """Everything the ontology document and its data element files get wrong (rules 1-6)."""
    base = base or root()
    defined = {c.get("concept"): c for c in onto.get("ontology") or []}
    named = {n: p for n, p in definitions.items() if n}
    out = [f"ontology: {e}" for e in ossie.validate(onto)]
    out += _naming_violations(defined)
    out += _definition_file_violations(definitions, base)
    out += _term_violations(defined, named, base)
    out += _ownership_violations(defined, named, domains, base)
    for name, paths in sorted(named.items()):
        out += _definition_content_violations(name, load_yaml(paths[0]), defined, classifications)
    return out


# The contracts (rules 7-11).


def _entity_link_violations(raw: dict, path: Path, where: str, entities: set[str]) -> list[str]:
    """Rule 11: a table's ``ontology`` link names an entity type, in a file that exists."""
    out = []
    for obj in raw.get("schema") or []:
        for link in obj.get("authoritativeDefinitions") or []:
            if link.get("type") != "ontology":
                continue
            entity = str(link.get("url", "")).partition("#")[2]
            if not _link_resolves(path, link):
                out.append(f"{where}: ontology link '{link.get('url')}' does not resolve")
            elif entity not in entities:
                out.append(
                    f"{where}: table '{obj.get('name')}' links '{entity}', which is not an "
                    "entity type of the ontology"
                )
    return out


def _column_violations(prop: dict, meaning: dict, concept: str, where: str) -> list[str]:
    """Rules 8-9: a linked column neither restates nor blanks its definition, nor contradicts its
    logicalType."""
    col = prop.get("name", "?")
    out = []
    for key in NOT_RESTATED:
        if key not in prop:
            continue
        if prop[key] in ("", None, []):
            out.append(
                f"{where}: column '{col}' sets an empty {key}, which hides the definition's. "
                "Remove it"
            )
        elif prop[key] == meaning.get(key):
            out.append(
                f"{where}: column '{col}' restates its definition's {key}. Remove the copy "
                "(it is inherited)"
            )
    local, defined = prop.get("logicalType"), meaning.get("logicalType")
    if local and defined and local != defined:
        out.append(
            f"{where}: column '{col}' is logicalType {local}, but definition '{concept}' is "
            f"{defined}"
        )
    return out


def _homonym_violations(
    by_name: dict[str, set[tuple[str, str | None]]],
) -> tuple[list[str], dict[str, list[str]]]:
    """Rule 10: a column name used in several contracts links a definition in every one of them.
    Two definitions under one name is an explicit homonym."""
    out: list[str] = []
    homonyms: dict[str, list[str]] = {}
    for col, uses in sorted(by_name.items()):
        if len({w for w, _ in uses}) < 2:
            continue
        linked = {c for _, c in uses}
        if None in linked:
            places = ", ".join(f"{w} ({c or 'no definition'})" for w, c in sorted(uses, key=str))
            out.append(
                f"column '{col}' appears in several contracts but is not linked in all of them: "
                f"{places}. Link each to the definition it means."
            )
        elif len(linked) > 1:
            homonyms[col] = sorted(linked)
    return out, homonyms


def contract_violations(
    base: Path, definitions: dict[str | None, list[Path]]
) -> tuple[list[str], dict]:
    """(violations, homonyms) across every contract. Homonyms: column name -> the 2+ definitions
    it links to."""
    out: list[str] = []
    meaning = {n: load_yaml(p[0]) for n, p in definitions.items() if n}
    used: set[str] = set()
    by_name: dict[str, set[tuple[str, str | None]]] = defaultdict(set)
    entities = {e["concept"] for e in registry.entity_types()}
    for path in _contracts(base):
        where = relative(path, base)
        try:
            odcs.load(path)
        except odcs.ResolutionError as e:
            out.append(f"{where}: {e}")
            continue
        raw = load_yaml(path)  # as written: rule 8 is about what the contract itself says
        out += _entity_link_violations(raw, path, where, entities)
        for prop in _properties(raw):
            col = prop.get("name", "?")
            out += [f"{where}: column '{col}': {v}" for v in _tag_value_errors(prop)]
            concept = registry.concept_of(prop, path)
            by_name[col].add((where, concept))
            resolved = [
                link
                for link in prop.get("authoritativeDefinitions") or []
                if link.get("type") in odcs.RESOLVABLE_TYPES
            ]
            if resolved and concept is None:  # rule 7
                out.append(
                    f"{where}: column '{col}' takes its meaning from outside the definitions "
                    f"({resolved[0].get('url')}). Link a contracts/definitions/ file as a "
                    "businessDefinition"
                )
            elif concept is not None and concept not in meaning:
                out.append(f"{where}: column '{col}' links unknown definition '{concept}'")
            elif concept is not None:
                used.add(concept)
                out += _column_violations(prop, meaning[concept], concept, where)
    out += [
        f"definition '{c}': no contract column links to it (remove it or link it)"
        for c in sorted(set(meaning) - used)
    ]
    homonym_errors, homonyms = _homonym_violations(by_name)
    return out + homonym_errors, homonyms


def main() -> int:
    documents = registry.ontology_files()
    if len(documents) != 1:
        found = f"expected one governance/ontology/*.ontology.yaml, found {len(documents)}"
        return report([found], name="Ontology", failed="Ontology violations", ok="")
    definitions = registry.definition_files()
    violations = ontology_violations(
        registry.ontology(),
        definitions,
        set(registry.domain_names()),
        set(registry.classification_values()),
    )
    contract_issues, homonyms = contract_violations(root(), definitions)
    linked = sum(
        1
        for p in _contracts(root())
        for prop in _properties(load_yaml(p))
        if registry.concept_of(prop, p)
    )
    code = report(
        violations + contract_issues,
        name="Ontology",
        failed="Ontology violations",
        ok=f"valid Ossie, {checks.plural(len(registry.entity_types()), 'entity type')}, "
        f"{checks.plural(len(definitions), 'definition')}, {linked} contract columns linked",
    )
    if code == 0:
        for col, concepts in homonyms.items():
            print(checks.note(f"homonym: {col} means {' or '.join(concepts)}, by contract"))
    return code


if __name__ == "__main__":
    sys.exit(main())
