#!/usr/bin/env python3
"""Federated governance policy: the business ontology, and every contract's links to it (ADR-0027).

The ontology's structure is one Apache Ossie document (``governance/ontology/*.ontology.yaml``);
each value type's meaning is an ODCS property that the domain owning it defines in
``domains/<domain>/contracts/definitions/<value_type>.odcs.yaml`` (shared places in the
reference domain), and every contract column that means it links there and inherits it.

  Ontology
    1. There is one ontology document, and Ossie's own validator passes it (vendored, unchanged:
       ``platform/context/semantics/vendor/ossie/``) — schema, unique names, references.
    2. Value types and data element files match one to one: each file names the value type it holds
       with an ODCS ``ontology`` link (``<…>.ontology.yaml#<ValueType>``) that resolves; no value
       type is defined twice. Governed-tag values (``privacyCategory``) are allowed values.
    3. Every entity type is a business term with a business definition (its description). A value
       type with a description is a business term too; one without is a data element only.
    4. Ownership comes from where things live, not from a list: a value type belongs to the domain
       whose folder defines it, an entity type to the domain that defines its identifying value
       types. That domain is registered, and it also defines every value type the entity type
       declares first.
    5. A definition is a valid ODCS property carrying meaning only — description, business name, a
       taxonomy classification, and a logicalType that agrees with what its value type extends;
       nothing table-specific (physical type, keys, required, quality…).
    6. Every definition is used by a contract column.

  Contracts (output ports and landing)
    7. A column takes its meaning only from a definition: any link datacontract-cli resolves
       (``businessDefinition`` — the ODCS type — or ``semantics``/``definition``) points at a
       definition file, and resolves.
    8. One place per fact: a linked column does not restate what its definition says, or blank it.
    9. A linked column's logicalType does not contradict its definition's.
    10. A column name used in two or more contracts links a definition in every one of them. Two
       definitions under one name is a homonym, and it is explicit: each column links its own.
    11. A table that declares the entity it represents (an ODCS ``ontology`` link on the schema
       object, ``…commerce.ontology.yaml#Order``) names an entity type of the ontology, in a file
       that exists.

A column may still set its own description or classification where the context changes it — a
customer's zip-code prefix is internal, the definition's public — and the contract's value wins.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import yaml
from dgp_platform_contracts import odcs
from open_data_contract_standard.model import SchemaProperty

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "platform" / "context" / "semantics"))
import ossie  # noqa: E402  (the shared Ossie reader; validates with Ossie's own validator)

from governance import registry  # noqa: E402

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
    "id", "name", "physicalType", "physicalName", "primaryKey", "primaryKeyPosition", "required",
    "unique", "partitioned", "partitionKeyPosition", "encryptedName", "transformSourceObjects",
    "transformLogic", "transformDescription", "relationships", "quality", "properties", "items",
    "map",
}  # fmt: skip
NOT_RESTATED = [
    "description", "businessName", "classification", "logicalTypeOptions", "synonyms", "examples",
]  # fmt: skip


def _contracts(root: Path) -> list[Path]:
    paths = [
        *root.glob("domains/*/contracts/**/*.odcs.yaml"),
    ]
    return sorted(p for p in paths if "definitions" not in p.parts)


def _properties(doc: dict):
    for obj in doc.get("schema") or []:
        yield from obj.get("properties") or []


def _tag_value_errors(prop: dict) -> list[str]:
    """A governed tag read from an ODCS custom property, with a value the tag does not allow."""
    custom = {c.get("property"): c.get("value") for c in prop.get("customProperties") or []}
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


def _identifying_values(entity: dict, values: set[str]) -> set[str]:
    rels = {r.get("name"): r for r in entity.get("relationships") or []}
    return {
        role.get("concept")
        for ident in entity.get("identify_by") or []
        for role in rels.get(ident, {}).get("roles") or []
    } & values


def ontology_violations(
    onto: dict,
    definitions: dict[str | None, list[Path]],
    domains: set[str],
    classifications: set[str],
    root: Path = REPO_ROOT,
) -> list[str]:
    out = [f"ontology: {e}" for e in ossie.validate(onto)]
    definitions = dict(definitions)
    for path in definitions.pop(None, []):
        out.append(
            f"{path.relative_to(root).as_posix()}: a data element must name the value type it "
            "holds (authoritativeDefinitions: [{type: ontology, url: <…>.ontology.yaml#<Type>}])"
        )
    for paths in definitions.values():
        for path in paths:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for link in doc.get("authoritativeDefinitions") or []:
                target = str(link.get("url", "")).partition("#")[0]
                if (
                    link.get("type") == "ontology"
                    and not (path.parent / target).resolve().is_file()
                ):
                    out.append(f"{path.relative_to(root).as_posix()}: ontology link "
                               f"'{link.get('url')}' does not resolve")  # fmt: skip
            out += [f"{path.relative_to(root).as_posix()}: {v}" for v in _tag_value_errors(doc)]
    defined = {c.get("concept"): c for c in onto.get("ontology") or []}
    values = {n for n, c in defined.items() if c.get("type") == "ValueType"}
    for name, c in sorted(defined.items()):
        if c.get("type") == "EntityType" and not str(c.get("description") or "").strip():
            out.append(f"entity type '{name}': needs a business definition (description)")
    for name, paths in sorted(definitions.items()):
        if len(paths) > 1:
            where = ", ".join(p.relative_to(root).as_posix() for p in paths)
            out.append(f"value type '{name}' is defined {len(paths)} times ({where}); define once")
    for name in sorted(values - set(definitions)):
        out.append(f"value type '{name}': no domain defines it (contracts/definitions/)")
    for name in sorted(set(definitions) - values):
        out.append(f"definition '{name}': no value type of that name in the ontology")

    owner_of = {n: registry.domain_of(p[0], root) for n, p in definitions.items()}
    first_declared: dict[str, str] = {}
    for name, entity in defined.items():
        if entity.get("type") != "EntityType":
            continue
        owners = {owner_of[v] for v in _identifying_values(entity, values) if v in owner_of}
        if len(owners) != 1 or not owners <= domains:
            out.append(
                f"entity type '{name}': its identifying value types must be defined by one "
                f"registered domain (found {sorted(owners) or 'none'})"
            )
        for r in entity.get("relationships") or []:
            for role in r.get("roles") or []:
                v = role.get("concept")
                if v not in values or first_declared.setdefault(v, name) != name:
                    continue
                if len(owners) == 1 and owner_of.get(v) not in (None, *owners):
                    (owner,) = owners
                    out.append(
                        f"value type '{v}' belongs to entity type '{name}' (domain {owner}) but "
                        f"is defined by {owner_of[v]}; move its definition to {owner}"
                    )
    for name in sorted(values - set(first_declared)):
        out.append(f"value type '{name}': no entity type declares a relationship to it")

    for name, paths in sorted(definitions.items()):
        prop = yaml.safe_load(paths[0].read_text(encoding="utf-8")) or {}
        try:
            SchemaProperty.model_validate(prop)
        except Exception as e:  # noqa: BLE001 — report any validation failure as a violation
            out.append(f"definition '{name}': not a valid ODCS property: {e}")
        if unknown := set(prop) - set(SchemaProperty.model_fields):
            out.append(f"definition '{name}': unknown ODCS property fields {sorted(unknown)}")
        if specific := set(prop) & TABLE_SPECIFIC:
            out.append(
                f"definition '{name}': {sorted(specific)} are table-specific; keep them on the "
                "contract column"
            )
        for key in ("description", "businessName"):
            if not str(prop.get(key) or "").strip():
                out.append(f"definition '{name}': needs a {key}")
        if prop.get("classification") not in classifications:
            out.append(
                f"definition '{name}': classification must be one of {sorted(classifications)}"
            )
        expected = LOGICAL_TYPES.get(_root_type(name, defined) or "")
        if name in values and prop.get("logicalType") != expected:
            out.append(
                f"definition '{name}': logicalType {prop.get('logicalType')} disagrees with the "
                f"ontology (extends {_root_type(name, defined)}, so {expected})"
            )
    return out


def contract_violations(
    root: Path, definitions: dict[str | None, list[Path]]
) -> tuple[list[str], dict]:
    """(violations, homonyms) — homonyms: column name -> the definitions it links to (2+)."""
    out: list[str] = []
    meaning = {
        n: yaml.safe_load(p[0].read_text(encoding="utf-8")) or {}
        for n, p in definitions.items()
        if n
    }
    used: set[str] = set()
    by_name: dict[str, set[tuple[str, str | None]]] = defaultdict(set)
    entity_types = {e["concept"] for e in registry.entity_types()}
    for path in _contracts(root):
        where = path.relative_to(root).as_posix()
        try:
            odcs.load(path)
        except odcs.ResolutionError as e:
            out.append(f"{where}: {e}")
            continue
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for obj in raw.get("schema") or []:
            for link in obj.get("authoritativeDefinitions") or []:
                if link.get("type") != "ontology":
                    continue
                target, _, entity = str(link.get("url", "")).partition("#")
                if not (path.parent / target).resolve().is_file():
                    out.append(f"{where}: ontology link '{link.get('url')}' does not resolve")
                elif entity not in entity_types:
                    out.append(
                        f"{where}: table '{obj.get('name')}' links '{entity}', which is not an "
                        "entity type of the ontology"
                    )
        for prop in _properties(raw):
            col = prop.get("name", "?")
            out += [f"{where}: column '{col}': {v}" for v in _tag_value_errors(prop)]
            concept = registry.concept_of(prop, path)
            by_name[col].add((where, concept))
            resolved = [
                ln
                for ln in prop.get("authoritativeDefinitions") or []
                if ln.get("type") in odcs.RESOLVABLE_TYPES
            ]
            if resolved and concept is None:
                out.append(
                    f"{where}: column '{col}' takes its meaning from outside the definitions "
                    f"({resolved[0].get('url')}); link a contracts/definitions/ file as a "
                    "businessDefinition"
                )
                continue
            if concept is None:
                continue
            if concept not in meaning:
                out.append(f"{where}: column '{col}' links unknown definition '{concept}'")
                continue
            used.add(concept)
            for key in NOT_RESTATED:
                if key not in prop:
                    continue
                if prop[key] in ("", None, []):
                    out.append(
                        f"{where}: column '{col}' sets an empty {key}, which hides the "
                        "definition's; remove it"
                    )
                elif prop[key] == meaning[concept].get(key):
                    out.append(
                        f"{where}: column '{col}' restates its definition's {key}; remove the "
                        "copy (it is inherited)"
                    )
            local, defined = prop.get("logicalType"), meaning[concept].get("logicalType")
            if local and defined and local != defined:
                out.append(
                    f"{where}: column '{col}' is logicalType {local}, but definition "
                    f"'{concept}' is {defined}"
                )
    for c in sorted(set(meaning) - used):
        out.append(f"definition '{c}': no contract column links to it (remove it or link it)")
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


def main() -> int:
    documents = sorted((REPO_ROOT / "governance/ontology").glob("*.ontology.yaml"))
    if len(documents) != 1:
        print(f"Ontology violations:\n  - expected one *.ontology.yaml, found {len(documents)}")
        return 1
    definitions = registry.definition_files()
    violations = ontology_violations(
        registry.ontology(),
        definitions,
        set(registry.domain_names()),
        set(registry.classification_values()),
    )
    contract_issues, homonyms = contract_violations(REPO_ROOT, definitions)
    violations += contract_issues
    if violations:
        print("Ontology violations:")
        for v in violations:
            print(f"  - {v}")
        return 1
    linked = sum(
        1
        for p in _contracts(REPO_ROOT)
        for prop in _properties(yaml.safe_load(p.read_text(encoding="utf-8")) or {})
        if registry.concept_of(prop, p)
    )
    print(
        f"Ontology OK: valid Ossie document, {len(registry.entity_types())} entity types, "
        f"{len(definitions)} definitions owned by their domains, {linked} contract columns linked."
    )
    for col, concepts in homonyms.items():
        print(f"  homonym: '{col}' means {' or '.join(concepts)}, depending on the contract")
    return 0


if __name__ == "__main__":
    sys.exit(main())
