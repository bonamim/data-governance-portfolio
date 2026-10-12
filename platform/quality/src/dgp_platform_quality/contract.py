"""Load an ODCS contract and turn its schema into executable column rules.

Only the subset the landing gate needs is parsed (required, unique, value range,
logical type). datacontract-cli validates the *contract itself* in CI; this engine
*executes* the rules the contract declares.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class ColumnRule:
    name: str
    logical_type: str | None = None
    required: bool = False
    unique: bool = False
    minimum: float | None = None
    maximum: float | None = None


@dataclass(frozen=True)
class Contract:
    id: str
    name: str
    version: str
    path: Path
    columns: list[ColumnRule] = field(default_factory=list)
    # File-completeness floor from the ODCS `quality` rowCount rule, if declared.
    min_rows: int | None = None

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]


def contract_path(repo_root: Path, domain: str, dataset: str) -> Path:
    """Where a domain's ODCS contract for a dataset lives."""
    base = (repo_root / "reference") if domain == "reference" else (repo_root / "domains" / domain)
    return base / "contracts" / f"{dataset}.odcs.yaml"


def _parse_column(prop: dict) -> ColumnRule:
    opts = prop.get("logicalTypeOptions") or {}
    return ColumnRule(
        name=prop["name"],
        logical_type=prop.get("logicalType"),
        required=bool(prop.get("required", False)),
        unique=bool(prop.get("unique", False)),
        minimum=opts.get("minimum"),
        maximum=opts.get("maximum"),
    )


def load_contract(path: Path) -> Contract:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
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
