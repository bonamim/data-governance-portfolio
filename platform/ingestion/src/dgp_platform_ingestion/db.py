"""Generic, config-driven Postgres load helpers.

Raw and operational tables are created with text columns from the CSV header.
Typing and cleaning happen in the dbt *staging* layer, which keeps the platform
loader generic: it never needs per-dataset schema knowledge. Identifiers are
quoted so a hostile header cannot inject SQL.
"""

from __future__ import annotations

import csv
from contextlib import contextmanager
from pathlib import Path
from typing import IO

import psycopg
from psycopg import sql

from .settings import PostgresSettings


@contextmanager
def connect(settings: PostgresSettings):
    with psycopg.connect(settings.conninfo(), autocommit=False) as conn:
        yield conn


def _read_header(csv_path: Path) -> list[str]:
    with csv_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        return next(reader)


def recreate_text_table(
    conn: psycopg.Connection, schema: str, table: str, columns: list[str]
) -> None:
    cols = sql.SQL(", ").join(sql.SQL("{} text").format(sql.Identifier(c)) for c in columns)
    ident = sql.Identifier(schema, table)
    conn.execute(sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(ident))
    conn.execute(sql.SQL("CREATE TABLE {} ({})").format(ident, cols))


def copy_csv_into(
    conn: psycopg.Connection, schema: str, table: str, source: IO[bytes] | IO[str]
) -> int:
    """COPY a CSV stream (with header) into an existing table; return row count."""
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


def load_csv_file(conn: psycopg.Connection, schema: str, table: str, csv_path: Path) -> int:
    """Create <schema>.<table> from the CSV header and load it. Returns row count."""
    columns = _read_header(csv_path)
    recreate_text_table(conn, schema, table, columns)
    with csv_path.open("rb") as fh:
        return copy_csv_into(conn, schema, table, fh)


def export_table_to_csv(conn: psycopg.Connection, schema: str, table: str, dest: Path) -> None:
    """COPY a whole table out to a CSV file (with header) for cross-instance copy."""
    ident = sql.Identifier(schema, table)
    copy_sql = sql.SQL("COPY {} TO STDOUT WITH (FORMAT csv, HEADER true)").format(ident)
    with dest.open("wb") as fh, conn.cursor() as cur, cur.copy(copy_sql) as cp:
        for data in cp:
            fh.write(data)
