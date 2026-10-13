#!/usr/bin/env python3
"""Write-audit-publish for materialized output ports (ADR-0006, proof P3).

For each domain, build with `--vars '{wap: true}'`: a table output port is written
to `<name>__candidate` and its contractual DQ runs against the candidate. Only if
the build (including those tests) passes do we **swap** the candidate into the
published name in one transaction. If it fails, the candidate is dropped and
consumers keep the last good version — a bad build never reaches them.

View ports have no materialized data to protect and are published in place.

Run: `make publish`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import psycopg
import yaml
from psycopg import sql

REPO_ROOT = Path(__file__).resolve().parents[2]
PROFILES = REPO_ROOT / "platform" / "transform"
DOMAINS = ["customer", "customer_experience", "sales"]  # dependency order


def _conninfo() -> str:
    host = os.environ.get("ANALYTICS_HOST", "localhost")
    port = os.environ.get("ANALYTICS_INTERNAL_PORT", os.environ.get("ANALYTICS_PORT", "5433"))
    db = os.environ.get("ANALYTICS_DB", "analytics")
    user = os.environ["ANALYTICS_SUPERUSER"]
    pw = os.environ["ANALYTICS_SUPERUSER_PASSWORD"]
    return f"host={host} port={port} dbname={db} user={user} password={pw}"


def _wap_products() -> list[str]:
    """Products that opt in to write-audit-publish (publish.audit == required, ADR-0014)."""
    out: list[str] = []
    for p in REPO_ROOT.glob("domains/*/data_products/*.yaml"):
        defn = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if (defn.get("publish") or {}).get("audit") == "required":
            out.append(defn["name"])
    return out


def _dbt_build_wap(domain: str, wap_products: list[str]) -> int:
    dbt = shutil.which("dbt") or "dbt"
    # Only the opted-in products are built to a __candidate; everything else in place.
    wap_vars = "{wap: true, wap_products: [" + ", ".join(wap_products) + "]}"
    proc = subprocess.run(
        [dbt, "build", "--vars", wap_vars, "--profiles-dir", str(PROFILES)],
        cwd=REPO_ROOT / "domains" / domain / "dbt",  # cwd so dbt-loom finds its config
    )
    return proc.returncode


def _candidates(conn: psycopg.Connection, domain: str) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT schemaname, tablename FROM pg_tables "
        "WHERE schemaname LIKE %s AND tablename LIKE %s",
        (f"{domain}\\_%", "%\\_\\_candidate"),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _grant() -> None:
    """Re-apply consumer grants (a swap/rebuild drops table grants)."""
    subprocess.run([sys.executable, str(REPO_ROOT / "platform" / "access" / "grant_consumers.py")])


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Write-audit-publish output ports.")
    parser.add_argument("--domain", help="publish only this domain (default: all, in order)")
    args = parser.parse_args()
    domains = [args.domain] if args.domain else DOMAINS

    wap_products = _wap_products()
    exit_code = 0
    with psycopg.connect(_conninfo(), autocommit=False) as conn:
        for domain in domains:
            rc = _dbt_build_wap(domain, wap_products)
            cands = _candidates(conn, domain)
            if rc == 0:
                for schema, cand in cands:
                    port = cand[: -len("__candidate")]
                    conn.execute(
                        sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(
                            sql.Identifier(schema, port)
                        )
                    )
                    conn.execute(
                        sql.SQL("ALTER TABLE {} RENAME TO {}").format(
                            sql.Identifier(schema, cand), sql.Identifier(port)
                        )
                    )
                    print(f"  published {schema}.{port} (audit passed, swapped)")
                conn.commit()
            else:
                for schema, cand in cands:
                    conn.execute(
                        sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(
                            sql.Identifier(schema, cand)
                        )
                    )
                conn.commit()
                print(
                    f"  QUARANTINED {domain}: audit failed — kept last good, dropped candidate(s)"
                )
                exit_code = 1
            # Grant after each domain so the next one can read its upstream ports.
            _grant()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
