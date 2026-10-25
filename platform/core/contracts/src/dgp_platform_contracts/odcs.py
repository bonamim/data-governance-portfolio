"""Read an ODCS contract with its linked definitions inlined — the one way the platform reads one.

A property can take its meaning from a shared definition instead of repeating it::

    - name: customer_id
      authoritativeDefinitions:
        - type: businessDefinition
          url: ../../sales/contracts/definitions/customer_key.odcs.yaml

The definition fills every element the property leaves unset (description, businessName,
logicalType, classification, tags, examples, …); anything the contract sets itself wins. So a
column inherits its meaning, and its classification, from one place, and every reader (masking,
grants, catalog, dbt sync, the semantic layer, the policy checks) sees the same resolved contract.

The rules follow datacontract-cli's resolver (its ``lint``/``test``/``export`` inline the same
way), and a conformance test pins them to it:

* resolvable link types, highest first: ``semantics``, ``semantic``, ``definition``,
  ``businessDefinition``; only the highest one on a property is followed;
* a link is a file in the repository, relative to the file holding it: either a definition file
  (``*.yaml``/``*.yml``/``*.json`` holding the property's elements) or one property of another
  contract (``<contract>#schema/<object>/properties/<property>``, matched on ``id`` then ``name``);
* a definition may link further (property -> shared definition -> concept); cycles are refused;
* ``id``, ``name``, ``authoritativeDefinitions``, ``properties`` and ``items`` are never inherited;
* nested ``properties`` and array ``items`` resolve too.

Unlike the CLI, a link to a URL is refused rather than fetched: definitions live in git, like
every other governed artefact (ADR-0018), so a contract resolves offline and the same everywhere.
A link that does not resolve rejects the contract — a reader never runs on half its meaning.

Pure pyyaml, so the lightweight runtimes (catalog generator, semantic MCP) can import it too.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path

import yaml

RESOLVABLE_TYPES = ("semantics", "semantic", "definition", "businessDefinition")
NOT_INHERITED = frozenset({"id", "name", "authoritativeDefinitions", "properties", "items"})
DEFINITION_SUFFIXES = frozenset({".yaml", ".yml", ".json"})

Reader = Callable[[Path], str]


class ResolutionError(ValueError):
    """A linked definition could not be resolved, so the contract is rejected."""


def _read_file(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load(path: Path, *, read: Reader | None = None) -> dict:
    """The contract at ``path`` as a dict, with every linked definition inlined."""
    read = read or _read_file
    path = Path(path).resolve()
    return resolve(yaml.safe_load(read(path)) or {}, path, read=read)


def linked_files(path: Path, *, read: Reader | None = None) -> set[Path]:
    """Every file the contract's links reach, transitively (not the contract itself): change one
    of them and the contract's meaning changes too."""
    read = read or _read_file
    seen: set[Path] = set()

    def recording(p: Path) -> str:
        seen.add(p)
        return read(p)

    load(path, read=recording)
    return seen - {Path(path).resolve()}


def resolve(doc: dict, location: Path, *, read: Reader | None = None) -> dict:
    """A copy of contract ``doc`` with its links inlined; ``location`` is where it lives, so its
    relative links resolve (``read`` lets a caller resolve it as of another git revision)."""
    return _Resolver(read or _read_file).contract(doc, Path(location).resolve(), frozenset())


class _Resolver:
    def __init__(self, read: Reader):
        self._read = read
        self._contracts: dict[Path, dict] = {}
        self._definitions: dict[str, dict] = {}

    def contract(self, doc: dict, location: Path, visited: frozenset[Path]) -> dict:
        doc = copy.deepcopy(doc)
        for obj in doc.get("schema") or []:
            for prop in obj.get("properties") or []:
                self._property(prop, location, visited)
        return doc

    def _property(self, prop: dict, location: Path, visited: frozenset[Path]) -> None:
        if isinstance(prop.get("items"), dict):
            self._property(prop["items"], location, visited)
        for nested in prop.get("properties") or []:
            self._property(nested, location, visited)
        url = _link(prop)
        if url is None:
            return
        definition = self._definition(url, location, visited)
        for key, value in definition.items():
            if key not in NOT_INHERITED and key not in prop:
                prop[key] = copy.deepcopy(value)

    def _definition(self, url: str, location: Path, visited: frozenset[Path]) -> dict:
        path_part, hash_, fragment = url.partition("#")
        if url.startswith(("http://", "https://")) or not (
            hash_ or Path(path_part).suffix.lower() in DEFINITION_SUFFIXES
        ):
            raise ResolutionError(
                f"{location}: link '{url}' is not a file in the repository; definitions live in "
                "git (a definition file, or <contract>#schema/<object>/properties/<property>)"
            )
        if not path_part or (hash_ and not fragment):
            raise ResolutionError(f"{location}: link '{url}' must be <file> or <file>#<fragment>")
        target = (location.parent / path_part).resolve()
        key = f"{target}#{fragment}"
        if key not in self._definitions:
            if fragment:
                found = _fragment(self._contract_at(target, url, visited), fragment, url)
            else:
                found = self._definition_file(target, url, visited)
            self._definitions[key] = found
        return self._definitions[key]

    def _contract_at(self, target: Path, url: str, visited: frozenset[Path]) -> dict:
        if target not in self._contracts:
            doc = self._parse(target, url, visited)
            if "schema" not in doc and "apiVersion" not in doc:
                raise ResolutionError(f"link '{url}': '{target}' is not a data contract")
            self._contracts[target] = self.contract(doc, target, visited | {target})
        return self._contracts[target]

    def _definition_file(self, target: Path, url: str, visited: frozenset[Path]) -> dict:
        doc = self._parse(target, url, visited)
        if "apiVersion" in doc and "schema" in doc:
            raise ResolutionError(
                f"link '{url}': '{target}' is a data contract, not a single property; add "
                "'#schema/<object>/properties/<property>' to say which property is meant"
            )
        self._property(doc, target, visited | {target})
        return doc

    def _parse(self, target: Path, url: str, visited: frozenset[Path]) -> dict:
        if target in visited:
            raise ResolutionError(f"link '{url}': '{target}' links back to itself (a cycle)")
        try:
            text = self._read(target)
        except (FileNotFoundError, OSError) as e:
            raise ResolutionError(f"link '{url}': '{target}' does not exist") from e
        doc = yaml.safe_load(text)
        if not isinstance(doc, dict):
            raise ResolutionError(f"link '{url}': '{target}' is not a YAML mapping")
        return doc


def _link(prop: dict) -> str | None:
    """The url of the highest-precedence resolvable link on ``prop``, or None."""
    links = prop.get("authoritativeDefinitions") or []
    for wanted in RESOLVABLE_TYPES:
        for link in links:
            if isinstance(link, dict) and link.get("type") == wanted and link.get("url"):
                return str(link["url"])
    return None


def _fragment(contract: dict, fragment: str, url: str) -> dict:
    """Walk ``schema/<object>/properties/<property>[/properties/<nested>|/items]…``."""
    steps = [s for s in fragment.split("/") if s]
    if len(steps) < 2 or steps[0] != "schema":
        raise ResolutionError(f"link '{url}': the fragment must start with 'schema/<object>'")
    current = _match(contract.get("schema") or [], steps[1], url)
    i = 2
    while i < len(steps):
        if steps[i] == "items":
            if not isinstance(current.get("items"), dict):
                raise ResolutionError(f"link '{url}': '{steps[i - 1]}' has no 'items'")
            current, i = current["items"], i + 1
        elif steps[i] == "properties" and i + 1 < len(steps):
            current, i = _match(current.get("properties") or [], steps[i + 1], url), i + 2
        else:
            raise ResolutionError(f"link '{url}': expected 'properties/<property>' or 'items'")
    if i == 2:
        raise ResolutionError(
            f"link '{url}': the fragment points at a schema object, not a property"
        )
    return current


def _match(candidates: list[dict], wanted: str, url: str) -> dict:
    for field in ("id", "name"):
        for c in candidates:
            if c.get(field) == wanted:
                return c
    names = ", ".join(str(c.get("id") or c.get("name")) for c in candidates)
    raise ResolutionError(f"link '{url}': no '{wanted}' (available: {names})")
