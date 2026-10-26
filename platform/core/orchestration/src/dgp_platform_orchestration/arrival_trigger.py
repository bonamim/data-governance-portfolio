"""Event-driven ingestion trigger (ADR-0016): landing arrivals -> a domain's arrival asset.

An Airflow 3 ``AssetWatcher`` runs this in the triggerer. It watches the arrival queue
(``landing_events.arrivals``, fed by the object store's bucket notifications) for the domain's
inbox prefixes and fires the domain's ``landing/<domain>`` asset, which schedules its ingestion
DAG. Fan-in is windowed: when something arrives it waits ``window_seconds`` for the rest of the
burst, then signals every due row at once — a thousand files are one event, not a thousand DAG
runs. Rows are stamped ``signalled_at`` (not deleted): the contract gate acks them once their file
has left the inbox, and a row still present after ``visibility_seconds`` is signalled again, so a
lost signal or a file still waiting on its completion signal is retried (at least once, like SQS).

In AWS the queue is SQS and this class is swapped for the common-messaging ``MessageQueueTrigger``;
the domain DAGs and the gate do not change.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

from airflow.triggers.base import BaseEventTrigger, TriggerEvent
from dgp_platform_ingestion import arrivals
from dgp_platform_ingestion.settings import PostgresSettings

log = logging.getLogger(__name__)


def _conninfo() -> str:
    """The queue role's connection, from the triggerer's environment (never serialized)."""
    return PostgresSettings.analytics(
        "landing_events", os.environ["LANDING_EVENTS_PASSWORD"]
    ).conninfo()


class LandingArrivalTrigger(BaseEventTrigger):
    def __init__(
        self,
        prefixes: list[str],
        window_seconds: int = 30,
        poll_seconds: int = 10,
        visibility_seconds: int = 600,
    ):
        super().__init__()
        self.prefixes = list(prefixes)
        self.window_seconds = window_seconds
        self.poll_seconds = poll_seconds
        self.visibility_seconds = visibility_seconds
        self._migrated = False

    def serialize(self) -> tuple[str, dict[str, Any]]:
        return (
            f"{type(self).__module__}.{type(self).__qualname__}",
            {
                "prefixes": self.prefixes,
                "window_seconds": self.window_seconds,
                "poll_seconds": self.poll_seconds,
                "visibility_seconds": self.visibility_seconds,
            },
        )

    def _mine(self, data: dict) -> list[str]:
        return [a.key for a in arrivals.parse(data) if a.key.startswith(tuple(self.prefixes))]

    async def _due(self, conn, lock: bool = False) -> list[tuple[int, list[str]]]:
        """Rows for these prefixes never signalled, or signalled longer ago than the timeout."""
        cur = await conn.execute(
            f"SELECT id, event_data FROM {arrivals.TABLE} WHERE signalled_at IS NULL"
            " OR signalled_at < now() - make_interval(secs => %s)"
            + (" FOR UPDATE SKIP LOCKED" if lock else ""),
            (self.visibility_seconds,),
        )
        return [(rid, keys) for rid, data in await cur.fetchall() if (keys := self._mine(data))]

    async def _ready(self, conn) -> bool:
        if self._migrated:
            return True
        cur = await conn.execute("SELECT to_regclass(%s)", (arrivals.TABLE,))
        if (await cur.fetchone())[0] is None:
            return False  # no event yet: the object store creates the table on its first one
        for stmt in arrivals.MIGRATIONS:  # once per trigger: ALTER TABLE locks the queue
            await conn.execute(stmt)
        await conn.commit()
        self._migrated = True
        return True

    async def run(self) -> AsyncIterator[TriggerEvent]:
        import psycopg

        while True:
            try:
                async with await psycopg.AsyncConnection.connect(_conninfo()) as conn:
                    if await self._ready(conn) and await self._due(conn):
                        await conn.commit()
                        await asyncio.sleep(self.window_seconds)  # let the burst finish
                        due = await self._due(conn, lock=True)
                        if due:
                            await conn.execute(
                                f"UPDATE {arrivals.TABLE} SET signalled_at = now()"
                                " WHERE id = ANY(%s)",
                                ([rid for rid, _ in due],),
                            )
                            await conn.commit()
                            keys = sorted({k for _, ks in due for k in ks})
                            log.info("arrivals for %s: %d file(s)", self.prefixes, len(keys))
                            yield TriggerEvent({"files": len(keys), "keys": keys[:50]})
                        else:
                            await conn.commit()
            except psycopg.OperationalError as exc:  # the warehouse restarting must not kill it
                log.warning("arrival queue unreachable: %s", exc)
            await asyncio.sleep(self.poll_seconds)
