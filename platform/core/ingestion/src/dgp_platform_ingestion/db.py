"""Generic, config-driven Postgres load helpers.

Raw and operational tables are created with text columns from the CSV header.
Typing and cleaning happen in the dbt *staging* layer, which keeps the platform
loader generic: it never needs per-dataset schema knowledge. Identifiers are
quoted so a hostile header cannot inject SQL.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

import psycopg
from psycopg import sql

from .settings import PostgresSettings


@contextmanager
def connect(settings: PostgresSettings) -> Iterator[psycopg.Connection]:
    with psycopg.connect(settings.conninfo(), autocommit=False) as conn:
        yield conn


def _read_header(csv_path: Path) -> list[str]:
    with csv_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        return next(reader)


def _existing_columns(conn: psycopg.Connection, schema: str, table: str) -> list[str] | None:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
        (schema, table),
    ).fetchall()
    return [r[0] for r in rows] or None


def prepare_text_table(
    conn: psycopg.Connection, schema: str, table: str, columns: list[str]
) -> None:
    """Make <schema>.<table> an empty text table with exactly ``columns``, ready for a reload.

    Same columns as today → TRUNCATE in place. The table keeps its identity, so everything built on
    it survives the reload: the dbt views up the medallion (several output ports are views), the
    masking views and the consumer grants. Inside the caller's transaction, readers keep seeing the
    previous rows until the new load commits. Only a real schema change (different columns) drops
    and recreates the table. CASCADE then removes the dependents, and the transform that follows
    the load rebuilds them against the new shape.
    """
    ident = sql.Identifier(schema, table)
    existing = _existing_columns(conn, schema, table)
    if existing == columns:
        conn.execute(sql.SQL("TRUNCATE {}").format(ident))
        return
    cols = sql.SQL(", ").join(sql.SQL("{} text").format(sql.Identifier(c)) for c in columns)
    conn.execute(sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(ident))
    conn.execute(sql.SQL("CREATE TABLE {} ({})").format(ident, cols))


def copy_csv_into(
    conn: psycopg.Connection, schema: str, table: str, source: IO[bytes] | IO[str]
) -> int:
    """COPY a CSV stream (with header) into an existing table, and return the row count."""
    ident = sql.Identifier(schema, table)
    copy_sql = sql.SQL("COPY {} FROM STDIN WITH (FORMAT csv, HEADER true)").format(ident)
    with conn.cursor() as cur:
        # The row count is only final once the COPY block has closed.
        with cur.copy(copy_sql) as cp:
            while True:
                chunk = source.read(1 << 20)
                if not chunk:
                    break
                cp.write(chunk)
        return cur.rowcount


def load_csv_file(
    conn: psycopg.Connection, schema: str, table: str, csv_path: Path, *, append: bool = False
) -> int:
    """Create <schema>.<table> from the CSV header and load it (or, with ``append``, add the rows
    of another file of the same batch to it). Returns row count."""
    columns = _read_header(csv_path)
    if not append:
        prepare_text_table(conn, schema, table, columns)
    with csv_path.open("rb") as fh:
        return copy_csv_into(conn, schema, table, fh)


def export_table_to_csv(conn: psycopg.Connection, schema: str, table: str, dest: Path) -> None:
    """COPY a whole table out to a CSV file (with header) for cross-instance copy."""
    ident = sql.Identifier(schema, table)
    copy_sql = sql.SQL("COPY {} TO STDOUT WITH (FORMAT csv, HEADER true)").format(ident)
    with dest.open("wb") as fh, conn.cursor() as cur, cur.copy(copy_sql) as cp:
        for data in cp:
            fh.write(data)
