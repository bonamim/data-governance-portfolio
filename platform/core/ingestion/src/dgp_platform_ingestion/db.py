"""Generic, config-driven Postgres load helpers.

Raw and operational tables are created with text columns from the CSV header.
Typing and cleaning happen in the dbt *staging* layer, which keeps the platform
loader generic: it never needs per-dataset schema knowledge. Identifiers are
quoted so a hostile header cannot inject SQL.

A raw table also carries the platform's system columns (``_system_*``, ADR-0028), after
the data columns: they are not in the file, so the caller names them and their types,
and the values every row of a file gets (``stamp``).
"""

from __future__ import annotations

import csv
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, LiteralString

import psycopg
from psycopg import sql

from .settings import PostgresSettings

# The column types a system column may have here, so none is ever built from free text.
_SYSTEM_TYPES: dict[str, LiteralString] = {"text": "text", "timestamptz": "timestamptz"}


@contextmanager
def connect(settings: PostgresSettings) -> Iterator[psycopg.Connection]:
    with psycopg.connect(settings.conninfo(), autocommit=False) as conn:
        yield conn


def csv_header(csv_path: Path) -> list[str]:
    """A CSV file's column names. A UTF-8 byte-order mark is not part of the first one: some
    exports (Olist's product_category_name_translation) start with one, and it used to end up in
    the column's name."""
    with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
        return next(csv.reader(fh))


def _existing_columns(conn: psycopg.Connection, schema: str, table: str) -> list[str] | None:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
        (schema, table),
    ).fetchall()
    return [r[0] for r in rows] or None


def prepare_text_table(
    conn: psycopg.Connection,
    schema: str,
    table: str,
    columns: list[str],
    system: dict[str, str] | None = None,
) -> None:
    """Make <schema>.<table> an empty text table with exactly ``columns``, then the ``system``
    columns ({name: SQL type}), ready for a reload.

    Same data columns as today → TRUNCATE in place, adding any system column the table lacks (a
    table from before it existed). The table keeps its identity, so everything built on it survives
    the reload: the dbt views up the medallion (several output ports are views), the masking views
    and the consumer grants. Inside the caller's transaction, readers keep seeing the previous rows
    until the new load commits. Only a real schema change (different data columns) drops and
    recreates the table. CASCADE then removes the dependents, and the transform that follows the
    load rebuilds them against the new shape.
    """
    system = system or {}
    ident = sql.Identifier(schema, table)
    existing = _existing_columns(conn, schema, table)
    if existing is not None and [c for c in existing if c not in system] == columns:
        conn.execute(sql.SQL("TRUNCATE {}").format(ident))
        for name, kind in system.items():
            if name not in existing:
                conn.execute(
                    sql.SQL("ALTER TABLE {} ADD COLUMN {} {}").format(
                        ident, sql.Identifier(name), sql.SQL(_SYSTEM_TYPES[kind])
                    )
                )
        return
    cols = [sql.SQL("{} text").format(sql.Identifier(c)) for c in columns]
    cols += [
        sql.SQL("{} {}").format(sql.Identifier(n), sql.SQL(_SYSTEM_TYPES[k]))
        for n, k in system.items()
    ]
    conn.execute(sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(ident))
    conn.execute(sql.SQL("CREATE TABLE {} ({})").format(ident, sql.SQL(", ").join(cols)))


def copy_csv_into(
    conn: psycopg.Connection,
    schema: str,
    table: str,
    source: IO[bytes] | IO[str],
    *,
    columns: list[str] | None = None,
    stamp: dict[str, object] | None = None,
) -> int:
    """COPY a CSV stream (with header) into an existing table, and return the row count.

    ``columns`` are the file's (the table's when omitted). ``stamp`` gives other columns a value
    for every row of this file: each becomes the column's default for the COPY alone, so the rows
    are written once, in one pass, and the default is dropped again before anything else runs."""
    ident = sql.Identifier(schema, table)
    target = (
        sql.SQL("{} ({})").format(ident, sql.SQL(", ").join(map(sql.Identifier, columns)))
        if columns
        else ident
    )
    copy_sql = sql.SQL("COPY {} FROM STDIN WITH (FORMAT csv, HEADER true)").format(target)
    stamp = stamp or {}
    for name, value in stamp.items():
        conn.execute(
            sql.SQL("ALTER TABLE {} ALTER COLUMN {} SET DEFAULT {}").format(
                ident, sql.Identifier(name), sql.Literal(value)
            )
        )
    with conn.cursor() as cur:
        # The row count is only final once the COPY block has closed.
        with cur.copy(copy_sql) as cp:
            while True:
                chunk = source.read(1 << 20)
                if not chunk:
                    break
                cp.write(chunk)
        rows = cur.rowcount
    for name in stamp:
        conn.execute(
            sql.SQL("ALTER TABLE {} ALTER COLUMN {} DROP DEFAULT").format(
                ident, sql.Identifier(name)
            )
        )
    return rows


def load_csv_file(
    conn: psycopg.Connection,
    schema: str,
    table: str,
    csv_path: Path,
    *,
    append: bool = False,
    governance: Callable[[], list[str]] | None = None,
    system: dict[str, str] | None = None,
    stamp: dict[str, object] | None = None,
) -> int:
    """Create <schema>.<table> from the CSV header and load it (or, with ``append``, add the rows
    of another file of the same batch to it). Returns row count.

    ``system`` adds the platform's columns ({name: SQL type}) after the file's, and ``stamp`` fills
    them for this file's rows. ``governance`` renders the statements that describe and tag the
    table. They run as soon as the table exists and before its first row is written, so the data is
    never in an ungoverned table, even on a warehouse whose DDL commits on its own (Snowflake)."""
    columns = csv_header(csv_path)
    if not append:
        prepare_text_table(conn, schema, table, columns, system)
        if governance is not None:
            execute_all(conn, governance())
    with csv_path.open("rb") as fh:
        return copy_csv_into(conn, schema, table, fh, columns=columns, stamp=stamp)


def columns_of(conn: psycopg.Connection, schema: str, table: str) -> set[str]:
    """The columns ``schema.table`` has (empty if it does not exist)."""
    return set(_existing_columns(conn, schema, table) or [])


def execute_all(conn: psycopg.Connection, statements: list[str]) -> None:
    """Run already-rendered statements in the caller's transaction (governance.statements quotes
    every identifier and literal it renders)."""
    for statement in statements:
        conn.execute(statement.encode())


def export_table_to_csv(conn: psycopg.Connection, schema: str, table: str, dest: Path) -> None:
    """COPY a whole table out to a CSV file (with header) for cross-instance copy."""
    ident = sql.Identifier(schema, table)
    copy_sql = sql.SQL("COPY {} TO STDOUT WITH (FORMAT csv, HEADER true)").format(ident)
    with dest.open("wb") as fh, conn.cursor() as cur, cur.copy(copy_sql) as cp:
        for data in cp:
            fh.write(data)
