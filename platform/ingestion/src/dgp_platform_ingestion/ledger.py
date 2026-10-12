"""The ingestion ledger — the platform's data-state record (ADR-0006).

Answers "which files and which source tables have been loaded?" and makes file
loads idempotent: a file already loaded with the same ETag is skipped (proof P6).
"""

from __future__ import annotations

import psycopg

LEDGER = "platform_meta.ingestion_ledger"


def file_already_loaded(conn: psycopg.Connection, source_identifier: str, etag: str) -> bool:
    row = conn.execute(
        f"SELECT 1 FROM {LEDGER} "
        "WHERE source_type = 'file' AND source_identifier = %s "
        "AND object_etag = %s AND status = 'loaded' LIMIT 1",
        (source_identifier, etag),
    ).fetchone()
    return row is not None


def record(
    conn: psycopg.Connection,
    *,
    source_type: str,
    source_identifier: str,
    target_schema: str,
    target_table: str,
    status: str,
    row_count: int | None = None,
    object_etag: str | None = None,
    contract_version: str | None = None,
    run_id: str | None = None,
    message: str | None = None,
) -> None:
    conn.execute(
        f"""
        INSERT INTO {LEDGER}
            (source_type, source_identifier, object_etag, contract_version,
             target_schema, target_table, row_count, status, run_id, message)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            source_type,
            source_identifier,
            object_etag,
            contract_version,
            target_schema,
            target_table,
            row_count,
            status,
            run_id,
            message,
        ),
    )
