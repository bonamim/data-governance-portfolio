"""Alerts to the owning domain (ADR-0016): quarantined deliveries, missing or incomplete ones.

The route is part of the domain's registry entry (``governance/domains/<domain>.yaml``)::

    alerts:
      channel: "#customer-data"            # where people look
      webhook_env: ALERT_WEBHOOK_CUSTOMER  # env var holding the webhook URL (a secret)

A webhook receives a Slack-compatible JSON body (``text`` plus structured fields). Without a
configured URL the alert is still logged and recorded. Every alert is written to
``platform_meta.alerts`` under a dedup key, so a condition that persists across runs (a partition
still missing an hour later) alerts once, not every run.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from pathlib import Path

import psycopg
import yaml
from psycopg.types.json import Jsonb

from .ledger import ALERTS

log = logging.getLogger("dgp.alerts")


def route(repo_root: Path, domain: str) -> dict:
    """The domain's alert route from the registry (empty if it declares none)."""
    path = repo_root / "governance" / "domains" / f"{domain}.yaml"
    entry = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    return (entry or {}).get("alerts") or {}


def send(
    conn: psycopg.Connection,
    repo_root: Path,
    *,
    domain: str,
    kind: str,
    subject: str,
    dedup_key: str,
    detail: dict | None = None,
) -> bool:
    """Record and deliver an alert; False if this dedup_key already alerted (nothing sent)."""
    r = route(repo_root, domain)
    url = os.environ.get(r.get("webhook_env", "")) if r.get("webhook_env") else None
    where = f"webhook {r['webhook_env']}" if url else f"log ({r.get('channel', 'no route')})"
    row = conn.execute(
        f"INSERT INTO {ALERTS} (dedup_key, domain, kind, subject, detail, route) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (dedup_key) DO NOTHING RETURNING id",
        (dedup_key, domain, kind, subject, Jsonb(detail or {}), where),
    ).fetchone()
    conn.commit()
    if row is None:
        return False
    log.warning("ALERT [%s] %s: %s", domain, kind, subject)
    delivered, error = (None, None)
    if url:
        body = {
            "text": f"[{domain}] {subject}",
            "domain": domain,
            "kind": kind,
            "channel": r.get("channel"),
            "detail": detail or {},
        }
        req = urllib.request.Request(  # noqa: S310 — the domain's configured alert route
            url,
            data=json.dumps(body, default=str).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 (configured route)
                delivered = 200 <= resp.status < 300
        except OSError as exc:  # an unreachable route must not fail the pipeline
            delivered, error = False, str(exc)
            log.error("alert delivery to %s failed: %s", r["webhook_env"], exc)
    conn.execute(
        f"UPDATE {ALERTS} SET delivered = %s, error = %s WHERE id = %s", (delivered, error, row[0])
    )
    conn.commit()
    return True
