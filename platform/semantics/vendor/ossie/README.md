# Vendored: Apache Ossie validator and schemas

Copied unchanged from [apache/ossie](https://github.com/apache/ossie) at commit
`fae8f416babfe9c996591bccf9515ecac482cc7e` (Apache License 2.0, headers kept), in Ossie's own
layout so `validation/validate.py` resolves the schemas it references offline:

| File | Upstream path |
|---|---|
| `validation/validate.py` | `validation/validate.py` — Ossie's official validator ("when the script is vendored elsewhere, point `--schema` at a copy of the schema") |
| `ontology/ontology.json` | `ontology/ontology.json` — ontology spec 0.2.0.dev0 |
| `ontology/mapping.json` | `ontology/mapping.json` |
| `core-spec/ossie-schema.json` | `core-spec/ossie-schema.json` — the semantic model spec the ontology schema references |

`governance/policies/check_ontology.py` and `platform/semantics/export_ontology.py` validate the
ontology with it (ADR-0027). Upgrading Ossie = re-copy these files from a newer commit and update
the commit above; never edit them here.
