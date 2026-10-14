#!/usr/bin/env python3
"""Adapter: publish the Ossie semantic models to Superset (ADR-0010, M6).

Reads every ``domains/*/semantics/*.semantic_model.yaml`` and, via the Superset API, ensures the
warehouse connection, a dataset per semantic dataset, and its metrics — generated from the one
governed Ossie definition, not hand-typed in the BI tool. Idempotent.

Run: ``make bi-publish`` (needs Superset up — ``make bi-up``).
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import requests
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SUPERSET = os.environ.get("SUPERSET_URL", "http://localhost:8088")
ADMIN = os.environ.get("SUPERSET_ADMIN", "admin")
ADMIN_PW = os.environ.get("SUPERSET_ADMIN_PASSWORD", "admin")
DB_NAME = "analytics (warehouse)"
# Superset reads the warehouse as a consumer role (output ports only), on the host-published port.
DB_URI = os.environ["SUPERSET_WAREHOUSE_URI"]


def _login() -> requests.Session:
    s = requests.Session()
    r = s.post(
        f"{SUPERSET}/api/v1/security/login",
        json={"username": ADMIN, "password": ADMIN_PW, "provider": "db", "refresh": True},
        timeout=30,
    )
    r.raise_for_status()
    s.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
    s.headers["X-CSRFToken"] = s.get(f"{SUPERSET}/api/v1/security/csrf_token/").json()["result"]
    s.headers["Referer"] = SUPERSET
    return s


def _find(s: requests.Session, kind: str, key: str, value: str) -> int | None:
    r = s.get(f"{SUPERSET}/api/v1/{kind}/", params={"page_size": 100}, timeout=30).json()
    for item in r.get("result", []):
        if item.get(key) == value:
            return item["id"]
    return None


def _ensure_database(s: requests.Session) -> int:
    db_id = _find(s, "database", "database_name", DB_NAME)
    if db_id:
        return db_id
    r = s.post(
        f"{SUPERSET}/api/v1/database/",
        json={"database_name": DB_NAME, "sqlalchemy_uri": DB_URI},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["id"]


def _metrics(spec: dict, dataset: str) -> list[dict]:
    out = []
    for m in spec.get("metrics") or []:
        expr = next(
            (
                d.get("expression", "")
                for d in (m.get("expression") or {}).get("dialects") or []
                if d.get("dialect") == "ANSI_SQL"
            ),
            "",
        )
        expr = re.sub(rf"\b{re.escape(dataset)}\.", "", expr)  # SUM(sales_performance.x) -> SUM(x)
        out.append(
            {
                "metric_name": m["name"],
                "expression": expr,
                "verbose_name": m["name"],
                "description": m.get("description", ""),
            }
        )
    return out


def _ensure_dataset(s: requests.Session, db_id: int, schema: str, table: str, metrics: list[dict]):
    ds_id = _find(s, "dataset", "table_name", table)
    if ds_id is None:
        r = s.post(
            f"{SUPERSET}/api/v1/dataset/",
            json={"database": db_id, "schema": schema, "table_name": table},
            timeout=60,
        )
        r.raise_for_status()
        ds_id = r.json()["id"]
    put = s.put(f"{SUPERSET}/api/v1/dataset/{ds_id}", json={"metrics": metrics}, timeout=60)
    put.raise_for_status()
    return ds_id


def main() -> int:
    models = sorted(REPO_ROOT.glob("domains/*/semantics/*.semantic_model.yaml"))
    if not models:
        print("No semantic models to publish.")
        return 0
    s = _login()
    db_id = _ensure_database(s)
    for m in models:
        spec = yaml.safe_load(m.read_text(encoding="utf-8")) or {}
        for ds in spec.get("datasets") or []:
            parts = str(ds.get("source", "")).split(".")
            if len(parts) != 3:
                continue
            _, schema, table = parts
            mets = _metrics(spec, ds["name"])
            _ensure_dataset(s, db_id, schema, table, mets)
            print(f"  published dataset {schema}.{table} with {len(mets)} metric(s) from {m.name}")
    print("Semantic models published to Superset.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
