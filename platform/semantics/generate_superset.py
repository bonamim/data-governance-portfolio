#!/usr/bin/env python3
"""Adapter: publish the Ossie semantic models to Superset (ADR-0010, M6).

Reads every ``domains/*/semantics/*.semantic_model.yaml`` and, via the Superset API, ensures the
warehouse connection, a dataset per semantic dataset, and its metrics — generated from the one
governed Ossie definition, not hand-typed in the BI tool. Idempotent.

Run: ``make bi-publish`` (needs Superset up — ``make bi-up``).
"""

from __future__ import annotations

import argparse
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
# The warehouse connection (consumer role, output ports only). Engine-agnostic: point this at
# Postgres or Snowflake etc. — the metric SQL below is ANSI, so it ports (see platform/semantics).
DB_URI = os.environ.get("SUPERSET_WAREHOUSE_URI")


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
    if not DB_URI:
        raise SystemExit("SUPERSET_WAREHOUSE_URI is required to create the warehouse connection.")
    r = s.post(
        f"{SUPERSET}/api/v1/database/",
        json={"database_name": DB_NAME, "sqlalchemy_uri": DB_URI},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["id"]


def _dataset_metrics(s: requests.Session, ds_id: int) -> dict[str, str]:
    r = s.get(f"{SUPERSET}/api/v1/dataset/{ds_id}", timeout=30).json()["result"]
    return {m["metric_name"]: m.get("expression", "") for m in r.get("metrics") or []}


def _norm(expr: str) -> str:
    return re.sub(r"\s+", "", expr or "").upper()


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


def _models() -> list[Path]:
    return sorted(REPO_ROOT.glob("domains/*/semantics/*.semantic_model.yaml"))


def _publish(s: requests.Session) -> int:
    db_id = _ensure_database(s)
    for m in _models():
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


def _check(s: requests.Session) -> int:
    """Fail if Superset's metrics drifted from the Ossie models (e.g. edited in the UI)."""
    drift: list[str] = []
    for m in _models():
        spec = yaml.safe_load(m.read_text(encoding="utf-8")) or {}
        for ds in spec.get("datasets") or []:
            parts = str(ds.get("source", "")).split(".")
            if len(parts) != 3:
                continue
            table = parts[2]
            expected = {x["metric_name"]: x["expression"] for x in _metrics(spec, ds["name"])}
            ds_id = _find(s, "dataset", "table_name", table)
            if ds_id is None:
                drift.append(f"{table}: not published to Superset — run `make bi-publish`")
                continue
            actual = _dataset_metrics(s, ds_id)
            for name, expr in expected.items():
                if name not in actual:
                    drift.append(f"{table}.{name}: missing in Superset")
                elif _norm(actual[name]) != _norm(expr):
                    drift.append(
                        f"{table}.{name}: expression drifted "
                        f"(Ossie '{expr}' vs Superset '{actual[name]}')"
                    )
            for name in actual.keys() - expected.keys():
                drift.append(f"{table}.{name}: extra metric in Superset, not in the Ossie model")
    if drift:
        print("Semantic drift FAILED — Superset does not match the Ossie models:")
        for d in drift:
            print(f"  - {d}")
        return 1
    print("Semantic drift OK: Superset metrics match the Ossie models.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish/verify Ossie models in Superset.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if Superset drifted from the Ossie models (don't publish)",
    )
    args = parser.parse_args()
    if not _models():
        print("No semantic models.")
        return 0
    s = _login()
    return _check(s) if args.check else _publish(s)


if __name__ == "__main__":
    sys.exit(main())
