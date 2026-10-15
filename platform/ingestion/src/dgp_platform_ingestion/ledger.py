"""The ingestion ledger — the platform's data-state record (ADR-0006).

Answers "which files and which source tables have been loaded?", records every landing-gate
outcome with its per-rule results (published to DataHub, ADR-0024), and makes file loads
idempotent: a delivery whose checksum equals the dataset's latest loaded file is skipped (P6).
"""

from __future__ import annotations

import psycopg
from psycopg.types.json import Jsonb

LEDGER = "platform_meta.ingestion_ledger"

# Columns added by ADR-0024. Fresh volumes get them from 00-init.sh; this keeps existing ones in
# step (platform_loader owns the table, so the loader can migrate it idempotently).
_MIGRATIONS = [
    f"ALTER TABLE {LEDGER} ADD COLUMN IF NOT EXISTS dataset text",
    f"ALTER TABLE {LEDGER} ADD COLUMN IF NOT EXISTS partition text",
    f"ALTER TABLE {LEDGER} ADD COLUMN IF NOT EXISTS object_version text",
    f"ALTER TABLE {LEDGER} ADD COLUMN IF NOT EXISTS archive_key text",
    f"ALTER TABLE {LEDGER} ADD COLUMN IF NOT EXISTS checks jsonb",
    # Idempotency is now "same checksum as the latest load of this dataset" (a partner may resend
    # an older file after a newer one; that must load again), so the per-key unique index goes.
    "DROP INDEX IF EXISTS platform_meta.ingestion_ledger_file_once",
    f"CREATE INDEX IF NOT EXISTS ingestion_ledger_dataset ON {LEDGER} (dataset, loaded_at DESC)",
]


def ensure_schema(conn: psycopg.Connection) -> None:
    for stmt in _MIGRATIONS:
        conn.execute(stmt)
    conn.commit()


def latest_loaded_etag(conn: psycopg.Connection, dataset: str) -> str | None:
    """Checksum of the file currently in the dataset's raw table (the most recent load)."""
    row = conn.execute(
        f"SELECT object_etag FROM {LEDGER} "
        "WHERE source_type = 'file' AND dataset = %s AND status = 'loaded' "
        "ORDER BY loaded_at DESC, id DESC LIMIT 1",
        (dataset,),
    ).fetchone()
    return row[0] if row else None


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
    dataset: str | None = None,
    partition: str | None = None,
    object_version: str | None = None,
    archive_key: str | None = None,
    checks: list[dict] | None = None,
) -> None:
    conn.execute(
        f"""
        INSERT INTO {LEDGER}
            (source_type, source_identifier, object_etag, contract_version,
             target_schema, target_table, row_count, status, run_id, message,
             dataset, partition, object_version, archive_key, checks)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
            dataset,
            partition,
            object_version,
            archive_key,
            Jsonb(checks) if checks is not None else None,
        ),
    )
