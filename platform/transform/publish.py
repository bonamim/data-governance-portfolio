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


def _wap_products() -> list[tuple[str, str]]:
    """(domain, product) for every product opting in to WAP (publish.audit == required)."""
    out: list[tuple[str, str]] = []
    for p in REPO_ROOT.glob("domains/*/data_products/*.yaml"):
        defn = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if (defn.get("publish") or {}).get("audit") == "required":
            out.append((p.relative_to(REPO_ROOT).parts[1], defn["name"]))
    return out


def _dbt_build_wap(domain: str, wap_names: list[str]) -> int:
    dbt = shutil.which("dbt") or "dbt"
    cwd = REPO_ROOT / "domains" / domain / "dbt"  # cwd so dbt-loom finds its config
    subprocess.run([dbt, "deps", "--profiles-dir", str(PROFILES)], cwd=cwd)  # shared macros
    # Only the opted-in products are built to a __candidate; everything else in place.
    wap_vars = "{wap: true, wap_products: [" + ", ".join(wap_names) + "]}"
    proc = subprocess.run(
        [dbt, "build", "--vars", wap_vars, "--profiles-dir", str(PROFILES)], cwd=cwd
    )
    return proc.returncode


def _candidates(conn: psycopg.Connection, domain: str) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT schemaname, tablename FROM pg_tables "
        "WHERE schemaname LIKE %s AND tablename LIKE %s",
        (f"{domain}\\_%", "%\\_\\_candidate"),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _dependents(conn: psycopg.Connection, schema: str, table: str) -> list[str]:
    """Views / materialized views that depend on <schema>.<table>.

    A blind ``DROP TABLE … CASCADE`` on the published port would silently drop these
    (and their grants) — a cross-domain consumer's view could vanish until its own DAG
    reruns. We refuse to swap when any exist rather than destroy them; rebuilding
    dependents is the per-model portable promote's job (ADR-0015).
    """
    if conn.execute("SELECT to_regclass(%s)", (f"{schema}.{table}",)).fetchone()[0] is None:
        return []  # first publish: no port to replace yet
    rows = conn.execute(
        """
        SELECT DISTINCT n.nspname || '.' || c.relname
        FROM pg_depend d
        JOIN pg_rewrite rw ON rw.oid = d.objid
        JOIN pg_class c ON c.oid = rw.ev_class
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE d.refobjid = to_regclass(%s) AND c.oid <> to_regclass(%s)
        """,
        (f"{schema}.{table}", f"{schema}.{table}"),
    ).fetchall()
    return [r[0] for r in rows]


def _grant() -> None:
    """Re-apply consumer grants (a swap/rebuild drops table grants)."""
    subprocess.run([sys.executable, str(REPO_ROOT / "platform" / "access" / "grant_consumers.py")])


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Write-audit-publish output ports.")
    parser.add_argument("--domain", help="publish only this domain (default: all, in order)")
    args = parser.parse_args()
    domains = [args.domain] if args.domain else DOMAINS

    wap_pairs = _wap_products()
    wap_names = [name for _, name in wap_pairs]
    exit_code = 0
    with psycopg.connect(_conninfo(), autocommit=False) as conn:
        for domain in domains:
            rc = _dbt_build_wap(domain, wap_names)
            cands = _candidates(conn, domain)
            cand_tables = {tbl for _, tbl in cands}

            # Guard (ADR-0014): a product that opted into WAP must have produced a
            # candidate. If not, its materialization is not a supported WAP target
            # (only full tables today) — fail loudly instead of silently unprotected.
            for d, product in wap_pairs:
                if d == domain and f"{product}__candidate" not in cand_tables:
                    raise RuntimeError(
                        f"{domain}.{product} sets publish.audit=required but no candidate was "
                        f"built — only full-table output ports support write-audit-publish "
                        f"today (ADR-0014). Remove the opt-in or make it a table."
                    )

            if rc == 0:
                for schema, cand in cands:
                    port = cand[: -len("__candidate")]
                    deps = _dependents(conn, schema, port)
                    if deps:
                        raise RuntimeError(
                            f"cannot swap {schema}.{port}: dependent object(s) "
                            f"{', '.join(deps)} would be lost. The full-table swap does not "
                            f"rebuild dependents — use the per-model portable promote (ADR-0015)."
                        )
                    # RESTRICT (not CASCADE): the pre-check above is authoritative, this is a
                    # defence-in-depth stop that errors rather than silently dropping dependents.
                    conn.execute(
                        sql.SQL("DROP TABLE IF EXISTS {} RESTRICT").format(
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
