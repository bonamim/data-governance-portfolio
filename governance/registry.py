"""Single source of truth for the mesh domain registry and the business ontology.

The domain list + metadata live in ``governance/domains/`` and the ontology (entities, concepts,
relationships) in ``governance/ontology/`` and the governed tags in ``governance/tags/``
(ADR-0027) — read from here
by the governance policy checks, the transform scripts, the catalog generator and the semantic MCP,
so nothing hardcodes the domain list, the taxonomy or a concept (which previously drifted across
files).
"""

from __future__ import annotations

from pathlib import Path

import yaml

_GOV = Path(__file__).resolve().parent  # governance/
ONTOLOGY_DIR = _GOV / "ontology"
REPO_ROOT = _GOV.parent


def domains() -> list[dict]:
    """Every registered mesh domain, as its registry dict (name, owner, description, …)."""
    out = []
    for p in sorted((_GOV / "domains").glob("*.yaml")):
        out.append(yaml.safe_load(p.read_text(encoding="utf-8")) or {})
    return out


def domain_names() -> list[str]:
    return [d["name"] for d in domains()]


def tags() -> dict[str, dict]:
    """Governed tags (governance/tags/<tag>.yaml): tag name -> its definition and allowed values."""
    out = {}
    for p in sorted((_GOV / "tags").glob("*.yaml")):
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        out[doc["tag"]] = doc
    return out


def classification_values() -> dict[str, dict]:
    """ODCS ``classification`` value -> its entry in the governed tag that reads it
    (``sensitivity``, SNOWFLAKE.TAGS.SENSITIVITY), most sensitive first."""
    (tag,) = [t for t in tags().values() if (t.get("odcs") or {}).get("field") == "classification"]
    return {v["value"]: v for v in tag["values"]}


def column_tags(prop: dict) -> dict[str, str]:
    """Governed tag -> value for a (resolved) contract column: each tag reads its ODCS field or
    custom property; a value outside the tag's allowed values is left out (the checks report it)."""
    out: dict[str, str] = {}
    custom = {c.get("property"): c.get("value") for c in prop.get("customProperties") or []}
    for name, tag in tags().items():
        where = tag.get("odcs") or {}
        value = (
            prop.get(where["field"])
            if "field" in where
            else custom.get(where.get("custom_property"))
        )
        if value in {v["value"] for v in tag["values"]}:
            out[name] = value
    return out


def ontology() -> dict:
    """The business ontology: its one Apache Ossie document (governance/ontology/)."""
    (path,) = ONTOLOGY_DIR.glob("*.ontology.yaml")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def ontology_link(doc: dict) -> str | None:
    """The ontology concept an ODCS element names with its ``ontology`` link
    (``<…>.ontology.yaml#<Concept>``) — a data element's value type, a table's entity type."""
    for link in doc.get("authoritativeDefinitions") or []:
        url = str(link.get("url", ""))
        if link.get("type") == "ontology" and ".ontology.yaml#" in url:
            return url.rsplit("#", 1)[1]
    return None


def definition_files(root: Path = REPO_ROOT) -> dict[str | None, list[Path]]:
    """Value type -> its data element file(s) in domains/<domain>/contracts/definitions/, keyed by
    the value type each file names with its ``ontology`` link (None: files that name none). A value
    type has exactly one; check_ontology reports duplicates and files without a link."""
    out: dict[str | None, list[Path]] = {}
    for p in sorted(root.glob("domains/*/contracts/definitions/*.odcs.yaml")):
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        out.setdefault(ontology_link(doc), []).append(p)
    return out


def domain_of(path: Path, root: Path = REPO_ROOT) -> str:
    """The domain a file belongs to, from where it lives (domains/<domain>/…)."""
    return path.resolve().relative_to(root.resolve()).parts[1]


def concept_file(concept: str) -> Path | None:
    """Where a value type's data element is defined, or None."""
    found = definition_files().get(concept)
    return found[0] if found else None


def concepts() -> dict[str, dict]:
    """Value type -> its data element (the ODCS property a linked column inherits)."""
    return {
        name: yaml.safe_load(paths[0].read_text(encoding="utf-8")) or {}
        for name, paths in definition_files().items()
        if name
    }


def concept_owners() -> dict[str, str]:
    """Value type -> the domain that defines it (where its definition file lives)."""
    return {name: domain_of(paths[0]) for name, paths in definition_files().items() if name}


def concept_entities() -> dict[str, str]:
    """Value type -> the entity type that declares a relationship to it first."""
    out: dict[str, str] = {}
    for c in ontology().get("ontology") or []:
        if c.get("type") == "EntityType":
            for rel in c.get("relationships") or []:
                for role in rel.get("roles") or []:
                    out.setdefault(role["concept"], c["concept"])
    return out


def entity_types() -> list[dict]:
    """Every entity type, with its ``owner``: the domain defining its identifying value types."""
    owners = concept_owners()
    out = []
    for c in ontology().get("ontology") or []:
        if c.get("type") != "EntityType":
            continue
        rels = {r["name"]: r for r in c.get("relationships") or []}
        ident = [role["concept"] for name in c.get("identify_by") or []
                 for role in rels.get(name, {}).get("roles") or []]  # fmt: skip
        domains_ = sorted({owners[v] for v in ident if v in owners})
        out.append({**c, "owner": domains_[0] if len(domains_) == 1 else None})
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
            return ontology_link(yaml.safe_load(target.read_text(encoding="utf-8")) or {})
    return None


def entity_of_table(obj: dict) -> str | None:
    """The entity type a contract's schema object (a table or file) represents — one row is one of
    it — from its ODCS ``ontology`` link, or None."""
    return ontology_link(obj)
