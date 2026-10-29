"""Govern a raw table from its landing contract: descriptions and governed tags, set by the loader.

The raw table is the first warehouse asset a consuming domain reads, and the one place where
restricted data sits unmasked. So the loader that writes it also writes what the contract says it
holds, as soon as the table exists and before its first row is written (``db.load_csv_file``), on
every load (a recreated table loses both). No row is ever in an ungoverned table, even on a
warehouse whose DDL commits on its own:

  * the table's and every column's description, resolved from the definitions (ADR-0027), as a
    ``COMMENT``. Any tool reading the warehouse finds them (the catalog ingest among them).
  * the platform's system columns (``_system_*``, governance/system_columns.yaml, ADR-0028), which
    every raw table carries whatever its source, with the platform's descriptions. A raw table
    without a landing contract (an OLTP source) is governed for those alone.
  * on a warehouse with native object tags (Snowflake), the governed tags (governance/tags/):
    every column's sensitivity and privacy category, the table's sensitivity. The values are the
    ones the dbt build sets on an output port (``dgp_governance.registry.warehouse_tags``).
    Postgres has no object tags: the catalog carries them (``catalog-govern``).

The statements are plain SQL text, rendered per warehouse, so they run on any DB-API connection
and can be reviewed without one (``dgp-ingest governance-preview``, the loader's counterpart of
``dbt run-operation dgp_dbt.governance_tags_preview``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from dgp_governance import registry
from dgp_platform_contracts import odcs

# Warehouses the loader can govern, and whether each has native object tags.
NATIVE_TAGS = {"postgres": False, "snowflake": True}
# The physical type of a system column, by its logicalType, per warehouse.
_TYPES = {
    "postgres": {"string": "text", "timestamp": "timestamptz"},
    "snowflake": {"string": "VARCHAR", "timestamp": "TIMESTAMP_TZ"},
}
_PLAIN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class RawGovernance:
    """What a raw table is told about itself: its landing contract, resolved."""

    description: str | None
    columns: dict[str, str | None] = field(default_factory=dict)  # column -> description
    table_tags: dict[str, str] = field(default_factory=dict)  # warehouse tag -> value
    column_tags: dict[str, dict[str, str]] = field(default_factory=dict)

    @classmethod
    def from_contract(cls, path: Path | None) -> RawGovernance:
        """The raw table's governance: its landing contract's (when it has one, resolved), then
        the platform's system columns."""
        schema = (odcs.load(path).get("schema") or [{}])[0] if path is not None else {}
        props = [p for p in schema.get("properties") or [] if p.get("name")]
        props += [dict(c) for c in registry.system_columns()]
        table_tags, column_tags = registry.warehouse_tags(props)
        return cls(
            description=schema.get("description"),
            columns={p["name"]: p.get("description") for p in props},
            table_tags=table_tags,
            column_tags=column_tags,
        )


def system_columns(warehouse: str) -> dict[str, str]:
    """The platform's system columns as the loader creates them on ``warehouse``: {name: type}."""
    types = _TYPES[warehouse]
    return {c["name"]: types[c.get("logicalType", "string")] for c in registry.system_columns()}


def statements(
    warehouse: str,
    schema: str,
    table: str,
    gov: RawGovernance,
    present: set[str] | None = None,
) -> list[str]:
    """The SQL that governs ``schema.table`` on ``warehouse``. ``present`` limits the columns to
    the ones the table has (None: every contract column)."""
    if warehouse not in NATIVE_TAGS:
        raise ValueError(f"no raw-table governance for {warehouse!r}, only {sorted(NATIVE_TAGS)}")
    pg = warehouse == "postgres"
    ident, literal = (_pg_ident, _pg_literal) if pg else (_sf_ident, _sf_literal)
    rel = f"{ident(schema)}.{ident(table)}"
    columns = [c for c in gov.columns if present is None or c in present]
    out = [f"COMMENT ON TABLE {rel} IS {literal(gov.description)}"]
    out += [f"COMMENT ON COLUMN {rel}.{ident(c)} IS {literal(gov.columns[c])}" for c in columns]
    if NATIVE_TAGS[warehouse]:
        out += [f"ALTER TABLE {rel} SET TAG {t} = {literal(v)}" for t, v in gov.table_tags.items()]
        out += [
            f"ALTER TABLE {rel} MODIFY COLUMN {ident(c)} SET TAG {t} = {literal(v)}"
            for c in columns
            for t, v in gov.column_tags.get(c, {}).items()
        ]
    return out


def _pg_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _pg_literal(text: str | None) -> str:
    return "NULL" if text is None else "'" + text.replace("'", "''") + "'"


def _sf_ident(name: str) -> str:
    # Unquoted, as dbt-snowflake names relations, so the names resolve the same way; anything
    # else is quoted so a column name can't inject SQL.
    return name if _PLAIN.match(name) else '"' + name.replace('"', '""') + '"'


def _sf_literal(text: str | None) -> str:
    # In a Snowflake string literal the backslash is an escape character too.
    return "NULL" if text is None else "'" + text.replace("\\", "\\\\").replace("'", "''") + "'"
