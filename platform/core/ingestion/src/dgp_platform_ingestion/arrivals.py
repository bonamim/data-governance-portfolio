"""The landing arrival queue (ADR-0016): object-store notifications -> Airflow, at least once.

Locally MinIO appends one row per ``s3:ObjectCreated`` event in the inbox to
``landing_events.arrivals`` (``event_time``, ``event_data`` in the S3 event format); in AWS the same
records arrive on an SQS queue. The semantics are SQS's:

  signal   Airflow's arrival trigger stamps unsignalled rows (``signalled_at``) and fires the
           domain's arrival asset — one event per window, however many files arrived
  ack      the contract gate deletes the rows of objects that have left the inbox (processed)
  redeliver a row still present after the visibility timeout (its file is still waiting on a
           completion signal, or a signal was lost) is signalled again

The gate drains the whole inbox regardless of which rows fired it, so a duplicate or lost signal
only changes *when* a file is processed, never *whether*.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import LiteralString, cast
from urllib.parse import unquote_plus

import psycopg

log = logging.getLogger("dgp.arrivals")

TABLE = "landing_events.arrivals"

# MinIO creates the table (event_time, event_data); the queue bookkeeping is ours.
MIGRATIONS = [
    f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS id bigint GENERATED ALWAYS AS IDENTITY",
    f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS signalled_at timestamptz",
    f"CREATE INDEX IF NOT EXISTS arrivals_signal ON {TABLE} (signalled_at)",
]


@dataclass(frozen=True)
class Arrival:
    key: str
    version_id: str | None
    size: int
    event_time: datetime | None
    principal: str | None


def parse(event_data: dict, event_time: datetime | None = None) -> list[Arrival]:
    """S3 event records -> arrivals (MinIO and AWS/SQS share the format)."""
    out = []
    for rec in event_data.get("Records") or []:
        obj = (rec.get("s3") or {}).get("object") or {}
        if not str(rec.get("eventName", "")).startswith(("s3:ObjectCreated", "ObjectCreated")):
            continue
        out.append(
            Arrival(
                key=unquote_plus(obj.get("key", "")),
                version_id=obj.get("versionId"),
                size=int(obj.get("size") or 0),
                event_time=event_time,
                principal=((rec.get("s3") or {}).get("bucket") or {})
                .get("ownerIdentity", {})
                .get("principalId"),
            )
        )
    return out


def ensure_schema(conn: psycopg.Connection) -> bool:
    """Add the queue columns; False when MinIO has not created the table yet (no events)."""
    row = conn.execute("SELECT to_regclass(%s)", (TABLE,)).fetchone()
    if row is None or row[0] is None:
        return False
    for stmt in MIGRATIONS:
        conn.execute(cast(LiteralString, stmt))  # constant DDL over constant names
    conn.commit()
    return True


def ack(conn: psycopg.Connection, prefixes: list[str], in_inbox: set[str]) -> int:
    """Delete the arrivals under ``prefixes`` whose object has left the inbox (processed).

    A key still in the inbox (waiting on a completion signal, or re-sent) keeps its rows, so it is
    signalled again after the visibility timeout.
    """
    if not ensure_schema(conn):
        return 0
    done = [
        rid
        for rid, data in conn.execute(f"SELECT id, event_data FROM {TABLE}").fetchall()
        if any(a.key.startswith(tuple(prefixes)) and a.key not in in_inbox for a in parse(data))
    ]
    if done:
        conn.execute(f"DELETE FROM {TABLE} WHERE id = ANY(%s)", (done,))
    conn.commit()
    return len(done)


def purge(conn: psycopg.Connection, days: int = 7) -> int:
    """Drop arrivals older than ``days`` (files outside every dataset prefix are never acked)."""
    if not ensure_schema(conn):
        return 0
    cur = conn.execute(
        f"DELETE FROM {TABLE} WHERE event_time < now() - make_interval(days => %s)", (days,)
    )
    conn.commit()
    return cur.rowcount
