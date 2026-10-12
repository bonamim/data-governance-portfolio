#!/usr/bin/env python3
"""Generate and apply consumer grants from the data product definitions.

Federated governance, executed by the platform (data-mesh.md section 4,
self-serve "Access" row): a consumer declared on a data product gets exactly
USAGE on the owning domain's output schema and SELECT on that one output port —
nothing else. Consumers therefore read output ports only, never raw or staging.

Run: `make grants` (or uv run python platform/access/grant_consumers.py).
Idempotent: GRANT is additive and safe to re-run.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
import yaml
from psycopg import sql

REPO_ROOT = Path(__file__).resolve().parents[2]


def _definition_files() -> list[Path]:
    return sorted(REPO_ROOT.glob("domains/*/data_products/*.yaml")) + sorted(
        REPO_ROOT.glob("reference/data_products/*.yaml")
    )


def _grants() -> list[tuple[str, str, str]]:
    """Return (consumer_role, owner_schema, product) for every declared consumer."""
    out: list[tuple[str, str, str]] = []
    for path in _definition_files():
        defn = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        owner_schema = (defn.get("owner") or {}).get("team")
        product = defn.get("name")
        for consumer in defn.get("consumers") or []:
            team = (consumer or {}).get("team")
            if not (owner_schema and product and team):
                continue
            if team == owner_schema:
                continue  # a domain already owns its own products
            out.append((f"{team}_role", owner_schema, product))
    return out


def _conninfo() -> str:
    host = os.environ.get("ANALYTICS_HOST", "localhost")
    port = os.environ.get("ANALYTICS_INTERNAL_PORT", os.environ.get("ANALYTICS_PORT", "5433"))
    db = os.environ.get("ANALYTICS_DB", "analytics")
    user = os.environ["ANALYTICS_SUPERUSER"]
    pw = os.environ["ANALYTICS_SUPERUSER_PASSWORD"]
    return f"host={host} port={port} dbname={db} user={user} password={pw}"


def main() -> int:
    grants = _grants()
    if not grants:
        print("No consumer grants declared.")
        return 0
    # Superuser applies grants (only the object owner or a superuser may GRANT).
    applied = 0
    with psycopg.connect(_conninfo(), autocommit=False) as conn:
        for role, schema, product in grants:
            # Skip products not built yet — grants are reapplied after each transform
            # run (a dbt rebuild drops table grants), so run this whenever is convenient.
            exists = conn.execute(
                "SELECT to_regclass(%s) IS NOT NULL", (f"{schema}.{product}",)
            ).fetchone()[0]
            if not exists:
                print(f"  skip {schema}.{product} (not built yet)")
                continue
            conn.execute(
                sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                    sql.Identifier(schema), sql.Identifier(role)
                )
            )
            conn.execute(
                sql.SQL("GRANT SELECT ON {} TO {}").format(
                    sql.Identifier(schema, product), sql.Identifier(role)
                )
            )
            print(f"  granted SELECT on {schema}.{product} to {role}")
            applied += 1
        conn.commit()
    print(f"Applied {applied} consumer grant(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
