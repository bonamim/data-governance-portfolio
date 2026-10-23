#!/usr/bin/env python3
"""Build the Ossie bundle — the business semantics as complete Apache Ossie documents (ADR-0027).

Source → compile → validate → deploy, as for dbt. The sources are written once and never repeat a
fact: the ontology's structure (``governance/ontology/*.ontology.yaml``), each value type's data
element (``domains/<d>/contracts/definitions/``), the semantic models (``domains/<d>/semantics/``)
and the contracts that link them. Ossie tools — Snowflake's
``SYSTEM$CREATE_SEMANTIC_VIEW_FROM_OSSIE_YAML``, an Ossie viewer, any converter — read a document as
it is, so the bundle compiles each source into a complete one, validates it with Ossie's own
validator, and is what gets deployed:

  build/ossie/<ontology>.ontology.yaml       the ontology, each value type with its description
  build/ossie/<model>.semantic_model.yaml    each semantic model compiled (ossie.compile): every
                                             contract-column field with its description and
                                             synonyms from the contract
  build/ossie/<model>.mapping.yaml           the mapping between them, generated from the
                                             contracts' ontology and data-element links

The documents reference each other by relative path, so the bundle deploys as a unit. Nothing in it
is edited by hand; the platform's own readers compile the same way (``ossie.load_compiled``).

Run: ``make ossie-build`` (writes ``build/ossie/``) · ``--check`` builds and validates only.
"""

from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

import yaml
from dgp_platform_quality import odcs

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
from ossie import compile, load, models, validate  # noqa: E402,A004  (the shared Ossie reader)

from governance.registry import (  # noqa: E402
    concept_of,
    concepts,
    entity_of_table,
    entity_types,
    ontology,
)

OUT = REPO_ROOT / "build" / "ossie"


def export() -> dict:
    """The Ossie document with each value type's description taken from its definition file."""
    doc = copy.deepcopy(ontology())
    meaning = concepts()
    for concept in doc.get("ontology") or []:
        # A business term keeps its business definition; a data element gets its definition's.
        defined = meaning.get(concept["concept"]) or {}
        if not concept.get("description") and defined.get("description"):
            concept["description"] = " ".join(str(defined["description"]).split())
    if errors := validate(doc):
        raise ValueError("exported ontology is not valid Ossie: " + "; ".join(errors))
    return doc


def mappings() -> dict[str, dict]:
    """Semantic model name -> its Ossie mapping onto the ontology, for the models with a dataset
    whose output port represents an entity type (an aggregate, one row per state, maps none)."""
    entities = {e["concept"]: e for e in entity_types()}
    out: dict[str, dict] = {}
    for model in sorted(REPO_ROOT.glob("domains/*/semantics/*.semantic_model.yaml")):
        spec = yaml.safe_load(model.read_text(encoding="utf-8")) or {}
        concept_mappings = []
        for ds in spec.get("datasets") or []:
            table = str(ds.get("source", "")).split(".")[-1]
            for contract in REPO_ROOT.glob(f"domains/*/contracts/{table}.odcs.yaml"):
                for obj in odcs.load(contract).get("schema") or []:  # links survive resolution
                    entity = entities.get(entity_of_table(obj) or "")
                    if not entity:
                        continue
                    held = {  # value type -> the column holding it
                        concept: p["name"]
                        for p in obj.get("properties") or []
                        if (concept := concept_of(p, contract))
                    }
                    rels = {r["name"]: r for r in entity.get("relationships") or []}
                    referents = [
                        {"relationship": name, "expression": f"{ds['name']}.{held[role]}"}
                        for name in entity.get("identify_by") or []
                        for role in [r["concept"] for r in rels[name].get("roles") or []]
                        if role in held
                    ]
                    if len(referents) != len(entity.get("identify_by") or []):
                        continue  # its identifier is not all in this dataset
                    children = [
                        {
                            "relationship": name,
                            "object_mapping": {
                                "concept": role,
                                "expression": f"{ds['name']}.{held[role]}",
                            },
                        }
                        for name, r in rels.items()
                        if name not in (entity.get("identify_by") or [])
                        for role in [x["concept"] for x in r.get("roles") or []]
                        if role in held
                    ]
                    mapping = {"concept": entity["concept"],
                               "object_mappings": [{"referent_mappings": referents}]}  # fmt: skip
                    if children:
                        mapping["link_mappings"] = [
                            {
                                # the same identifier, written out again (no YAML alias)
                                "object_mapping": {"referent_mappings": copy.deepcopy(referents)},
                                "children": children,
                            }
                        ]
                    concept_mappings.append(mapping)
        if not concept_mappings:
            continue
        name = spec["name"]
        out[name] = {
            "version": "0.2.0.dev0",
            "name": f"{name}_mapping",
            "description": f"Maps the {ontology()['name']} ontology onto the {name} semantic model "
            "(generated from the contracts' ontology and data-element links, ADR-0027).",
            "ontology_ref": {
                "name": ontology()["name"],
                "iri": f"./{ontology()['name']}.ontology.yaml",
            },
            "semantic_model_ref": {"name": name, "iri": f"./{name}.semantic_model.yaml"},
            "concept_mappings": concept_mappings,
        }
        if errors := validate(out[name], "ontology/mapping.json"):
            raise ValueError(
                f"generated mapping for {name} is not valid Ossie: " + "; ".join(errors)
            )
    return out


def semantic_models() -> dict[str, dict]:
    """Semantic model name -> the model compiled into a complete Ossie document, validated."""
    out = {}
    for path in models():
        compiled = compile(load(path))
        if errors := validate(compiled, "core-spec/ossie-schema.json"):
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
        print(f"Ossie bundle OK: {len(files)} valid Ossie documents ({', '.join(sorted(files))}).")
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    for stale in OUT.glob("*.yaml"):
        if stale.name not in files:
            stale.unlink()
    for name, doc in files.items():
        (OUT / name).write_text(yaml.safe_dump(doc, sort_keys=False, width=100), encoding="utf-8")
        print(f"  wrote {(OUT / name).relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
