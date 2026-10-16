"""The ingestion ledger — the platform's data-state record (ADR-0006).

Answers "which files and which source tables have been loaded?", records every landing-gate
outcome with its per-rule results (published to DataHub, ADR-0024), and makes file loads
idempotent: a delivery whose content equals the dataset's latest gated file is skipped (P6).

It is also the hand-off between the two landing steps (ADR-0025): the contract gate writes an
``accepted`` row; the raw load writes a ``loaded`` (or ``superseded``) row whose ``gate_id`` points
back at it. An ``accepted`` row nothing points at is waiting to be loaded.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg
from psycopg.types.json import Jsonb

LEDGER = "platform_meta.ingestion_ledger"

STATUSES = ("accepted", "loaded", "quarantined", "skipped", "superseded")

# Columns added by ADR-0024 and ADR-0025. Fresh volumes get them from 00-init.sh; this keeps
# existing ones in step (platform_loader owns the table, so the loader can migrate it idempotently).
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
    # ADR-0025: the gate and the load are separate steps, joined by gate_id.
    f"ALTER TABLE {LEDGER} ADD COLUMN IF NOT EXISTS archive_version text",
    f"ALTER TABLE {LEDGER} ADD COLUMN IF NOT EXISTS gate_id bigint",
    f"CREATE INDEX IF NOT EXISTS ingestion_ledger_gate ON {LEDGER} (gate_id)",
    # Dedup on content, not the ETag: the ETag of the same bytes differs between a single-part
    # and a multipart upload, so a re-delivery by another client would load again.
    f"ALTER TABLE {LEDGER} ADD COLUMN IF NOT EXISTS content_sha256 text",
    # Widen the status check once (to accepted/superseded), not on every run.
    f"""DO $$ BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ingestion_ledger_status_check'
                     AND pg_get_constraintdef(oid) LIKE '%superseded%') THEN
        ALTER TABLE {LEDGER} DROP CONSTRAINT IF EXISTS ingestion_ledger_status_check;
        ALTER TABLE {LEDGER} ADD CONSTRAINT ingestion_ledger_status_check
          CHECK (status IN {STATUSES!r});
      END IF;
    END $$""",
]


def ensure_schema(conn: psycopg.Connection) -> None:
    for stmt in _MIGRATIONS:
        conn.execute(stmt)
    conn.commit()


def unchanged(conn: psycopg.Connection, dataset: str, sha256: str, etag: str) -> bool:
    """Is this delivery the same content as the dataset's newest good file (the one in raw, or
    the one about to be)? Compared by SHA-256; rows written before it was recorded fall back to
    the ETag."""
    row = conn.execute(
        f"SELECT content_sha256, object_etag FROM {LEDGER} "
        "WHERE source_type = 'file' AND dataset = %s AND status IN ('accepted', 'loaded') "
        "ORDER BY id DESC LIMIT 1",
        (dataset,),
    ).fetchone()
    if row is None:
        return False
    return row[0] == sha256 if row[0] else row[1] == etag


@dataclass(frozen=True)
class Accepted:
    """An ``accepted`` ledger row: a gated file in the archive, not yet loaded."""

    id: int
    source_identifier: str
    object_etag: str | None
    content_sha256: str | None
    object_version: str | None
    contract_version: str | None
    partition: str | None
    archive_key: str
    archive_version: str | None


def pending(conn: psycopg.Connection, dataset: str) -> list[Accepted]:
    """Accepted files of a dataset that no load has picked up yet, oldest first."""
    rows = conn.execute(
        f"SELECT a.id, a.source_identifier, a.object_etag, a.content_sha256, a.object_version, "
        "a.contract_version, "
        f"a.partition, a.archive_key, a.archive_version FROM {LEDGER} a "
        "WHERE a.dataset = %s AND a.status = 'accepted' "
        f"AND NOT EXISTS (SELECT 1 FROM {LEDGER} l WHERE l.gate_id = a.id) ORDER BY a.id",
        (dataset,),
    ).fetchall()
    return [Accepted(*r) for r in rows]


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
    archive_version: str | None = None,
    gate_id: int | None = None,
    content_sha256: str | None = None,
) -> None:
    conn.execute(
        f"""
        INSERT INTO {LEDGER}
            (source_type, source_identifier, object_etag, contract_version,
             target_schema, target_table, row_count, status, run_id, message,
             dataset, partition, object_version, archive_key, checks, archive_version, gate_id,
             content_sha256)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
            archive_version,
            gate_id,
            content_sha256,
        ),
    )
