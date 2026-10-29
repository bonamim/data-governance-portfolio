#!/usr/bin/env python3
"""Build the Ossie bundle: the business semantics as complete Apache Ossie documents (ADR-0027).

Source → compile → validate → deploy, as for dbt. The sources are written once and never repeat a
fact: the ontology's structure (``governance/ontology/*.ontology.yaml``), each value type's data
element (``domains/<d>/contracts/dictionary/``), the semantic models (``domains/<d>/semantics/``)
and the contracts that link them. Ossie tools (Snowflake's
``SYSTEM$CREATE_SEMANTIC_VIEW_FROM_OSSIE_YAML``, an Ossie viewer, any converter) read a document as
it is, so the bundle compiles each source into a complete one, validates it with Ossie's own
validator, and is what gets deployed:

  build/ossie/<ontology>.ontology.yaml       the ontology, each value type with its description
  build/ossie/<model>.semantic_model.yaml    each semantic model, compiled (compile_model): every
                                             contract-column field with its description and
                                             synonyms from the contract
  build/ossie/<model>.mapping.yaml           the mapping between them, generated from the
                                             contracts' ontology and data-element links

The documents reference each other by relative path, so the bundle deploys as a unit. Nothing in it
is edited by hand, and the platform's own readers compile the same way (``ossie.load_compiled``).

Run: ``make ossie-build`` (writes ``build/ossie/``) · ``--check`` builds and validates only.
"""

from __future__ import annotations

import argparse
import copy
import sys

import yaml
from dgp_governance import checks, products, registry
from dgp_governance.repo import relative, root
from dgp_platform_semantics import ossie

MAPPING_VERSION = "0.2.0.dev0"


def export() -> dict:
    """The ontology as an Ossie document, each value type with the description its data element
    gives it (a business term keeps its own business definition)."""
    doc = copy.deepcopy(registry.ontology())
    meaning = registry.concepts()
    for concept in doc.get("ontology") or []:
        defined = meaning.get(concept["concept"]) or {}
        if not concept.get("description") and defined.get("description"):
            concept["description"] = " ".join(str(defined["description"]).split())
    if errors := ossie.validate(doc):
        raise ValueError("exported ontology is not valid Ossie: " + "; ".join(errors))
    return doc


def _concept_mapping(dataset: str, entity: dict, held: dict[str, str]) -> dict | None:
    """How one dataset maps an entity type, given which column holds each value type, or None
    when the entity's identifier is not all in the dataset."""
    relationships = {r["name"]: r for r in entity.get("relationships") or []}
    identify_by = entity.get("identify_by") or []

    def expression(concept: str) -> str:
        return f"{dataset}.{held[concept]}"

    def roles(name: str) -> list[str]:
        return [
            r["concept"] for r in relationships[name].get("roles") or [] if r["concept"] in held
        ]

    referents = [
        {"relationship": name, "expression": expression(role)}
        for name in identify_by
        for role in roles(name)
    ]
    if len(referents) != len(identify_by):
        return None
    children = [
        {"relationship": name, "object_mapping": {"concept": role, "expression": expression(role)}}
        for name in relationships
        if name not in identify_by
        for role in roles(name)
    ]
    mapping = {"concept": entity["concept"], "object_mappings": [{"referent_mappings": referents}]}
    if children:
        # The same identifier, written out again: Ossie documents use no YAML aliases.
        identifier = {"referent_mappings": copy.deepcopy(referents)}
        mapping["link_mappings"] = [{"object_mapping": identifier, "children": children}]
    return mapping


def _dataset_mappings(dataset: dict, entities: dict[str, dict]) -> list[dict]:
    """The entity types a dataset represents, mapped through its output port's column links."""
    port = products.output_port(ossie.source_table(dataset))
    entity = entities.get(registry.entity_of_table(port.schema) or "") if port else None
    if entity is None:
        return []
    held = {  # value type -> the column holding it
        concept: column["name"]
        for column in port.columns
        if (concept := registry.concept_of(column, port.contract))
    }
    mapping = _concept_mapping(dataset["name"], entity, held)
    return [mapping] if mapping else []


def mappings() -> dict[str, dict]:
    """Semantic model name -> its Ossie mapping onto the ontology, for every model with a dataset
    whose output port represents an entity type (an aggregate, one row per state, maps none)."""
    onto = registry.ontology()["name"]
    entities = {e["concept"]: e for e in registry.entity_types()}
    out: dict[str, dict] = {}
    for path in ossie.models():
        spec = ossie.load(path)
        concept_mappings = [
            m for dataset in ossie.datasets(spec) for m in _dataset_mappings(dataset, entities)
        ]
        if not concept_mappings:
            continue
        name = spec["name"]
        out[name] = {
            "version": MAPPING_VERSION,
            "name": f"{name}_mapping",
            "description": f"Maps the {onto} ontology onto the {name} semantic model "
            "(generated from the contracts' ontology and data-element links, ADR-0027).",
            "ontology_ref": {"name": onto, "iri": f"./{onto}.ontology.yaml"},
            "semantic_model_ref": {"name": name, "iri": f"./{name}.semantic_model.yaml"},
            "concept_mappings": concept_mappings,
        }
        if errors := ossie.validate(out[name], ossie.MAPPING_SCHEMA):
            raise ValueError(
                f"generated mapping for {name} is not valid Ossie: " + "; ".join(errors)
            )
    return out


def semantic_models() -> dict[str, dict]:
    """Semantic model name -> the model compiled into a complete Ossie document, validated."""
    out = {}
    for path in ossie.models():
        compiled = ossie.load_compiled(path)
        if errors := ossie.validate(compiled, ossie.MODEL_SCHEMA):
            raise ValueError(f"compiled {path.name} is not valid Ossie: " + "; ".join(errors))
        out[compiled["name"]] = compiled
    return out


def bundle() -> dict[str, dict]:
    """File name -> document: the whole bundle, every document validated."""
    doc = export()
    files = {f"{doc['name']}.ontology.yaml": doc}
    files |= {f"{name}.semantic_model.yaml": m for name, m in semantic_models().items()}
    files |= {f"{name}.mapping.yaml": m for name, m in mappings().items()}
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="build and validate, write nothing")
    args = parser.parse_args()
    files = bundle()
    if args.check:
        print(checks.passed("Ossie bundle", f"{checks.plural(len(files), 'valid document')}"))
        return 0
    out = root() / "build" / "ossie"
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.glob("*.yaml"):
        if stale.name not in files:
            stale.unlink()
    for name, doc in files.items():
        (out / name).write_text(yaml.safe_dump(doc, sort_keys=False, width=100), encoding="utf-8")
        print(f"  wrote {relative(out / name)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
