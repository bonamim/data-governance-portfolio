"""Load an ODCS contract and turn its schema into executable column rules.

Only the subset the landing gate needs is parsed (required, unique, value range,
logical type). datacontract-cli validates the *contract itself* in CI, and this engine
*executes* the rules the contract declares.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from dgp_platform_contracts import odcs


@dataclass(frozen=True)
class ColumnRule:
    name: str
    logical_type: str | None = None
    required: bool = False
    unique: bool = False
    minimum: float | None = None
    maximum: float | None = None
    # What the column means, its own or inherited from its definition (ADR-0027).
    description: str | None = None


@dataclass(frozen=True)
class Contract:
    id: str
    name: str
    version: str
    path: Path
    columns: list[ColumnRule] = field(default_factory=list)
    # File-completeness floor from the ODCS `quality` rowCount rule, if declared.
    min_rows: int | None = None
    # ODCS `customProperties` as {property: value}: the landing operational rules
    # (partitioning, file naming, retention, completeness signal, ADR-0024).
    properties: dict = field(default_factory=dict)
    team: str | None = None
    purpose: str | None = None
    # What the file holds: the schema object's description (one row per what).
    description: str | None = None
    # ODCS `slaProperties` as {property: {value, unit, element}}, e.g. timeOfAvailability, the
    # delivery deadline the landing SLA monitor checks (ADR-0016).
    sla: dict = field(default_factory=dict)

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]


def contract_path(repo_root: Path, domain: str, dataset: str) -> Path:
    """Where a domain's landing (ingress) ODCS contract for a dataset lives."""
    return repo_root / "domains" / domain / "contracts" / "landing" / f"{dataset}.odcs.yaml"


def source_contract_path(repo_root: Path, domain: str, dataset: str, source_type: str) -> Path:
    """Where the contract of what a source delivers lives, by how it arrives: a landing file's in
    ``contracts/landing/``, an operational database table's in ``contracts/oltp/``. Either
    governs the raw table the platform loads it into."""
    folder = {"landing": "landing", "oltp": "oltp"}[source_type]
    return repo_root / "domains" / domain / "contracts" / folder / f"{dataset}.odcs.yaml"


def _parse_column(prop: dict) -> ColumnRule:
    opts = prop.get("logicalTypeOptions") or {}
    return ColumnRule(
        name=prop["name"],
        logical_type=prop.get("logicalType"),
        required=bool(prop.get("required", False)),
        unique=bool(prop.get("unique", False)),
        minimum=opts.get("minimum"),
        maximum=opts.get("maximum"),
        description=prop.get("description"),
    )


def load_contract(path: Path) -> Contract:
    raw = odcs.load(path)
    schemas = raw.get("schema") or []
    if not schemas:
        raise ValueError(f"{path}: ODCS contract has no schema")
    schema = schemas[0]
    properties = schema.get("properties") or []
    return Contract(
        id=raw.get("id", path.stem),
        name=raw.get("name", path.stem),
        version=str(raw.get("version", "")),
        path=path,
        columns=[_parse_column(p) for p in properties],
        min_rows=_min_rows(schema.get("quality") or []),
        properties={
            p["property"]: p.get("value")
            for p in raw.get("customProperties") or []
            if p.get("property")
        },
        team=(raw.get("team") or {}).get("name"),
        purpose=(raw.get("description") or {}).get("purpose"),
        description=schema.get("description"),
        sla={
            p["property"]: {k: v for k, v in p.items() if k != "property"}
            for p in raw.get("slaProperties") or []
            if p.get("property")
        },
    )


def _min_rows(quality_rules: list[dict]) -> int | None:
    """Read the file-completeness floor from an ODCS library rowCount rule."""
    for rule in quality_rules:
        if rule.get("type") == "library" and rule.get("metric") == "rowCount":
            bound = rule.get("mustBeGreaterThan")
            if bound is not None:
                return int(bound)
    return None


def load_contract_for(repo_root: Path, domain: str, dataset: str) -> Contract | None:
    """Return the dataset's contract, or None if the dataset has none yet."""
    path = contract_path(repo_root, domain, dataset)
    return load_contract(path) if path.exists() else None
