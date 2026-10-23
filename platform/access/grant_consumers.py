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
from dgp_platform_quality import odcs
from psycopg import sql

REPO_ROOT = Path(__file__).resolve().parents[2]


def _definition_files() -> list[Path]:
    return sorted(REPO_ROOT.glob("domains/*/data_products/*.yaml"))


def port_location(owner: str, product: str) -> tuple[str, str] | None:
    """Physical (schema, table) of an output port, derived from its dbt model file.

    A port is declared by its ODCS contract (tag output_port); its physical home is
    the medallion layer folder its dbt model lives in, so the schema is
    <owner>_<layer> (Option B / ADR-0012). No build needed.
    """
    models = REPO_ROOT / "domains" / owner / "dbt" / "models"
    for path in models.rglob(f"{product}.sql"):
        layer = path.parent.name  # bronze | silver | gold
        return f"{owner}_{layer}", product
    return None


# Least-privilege read-only role the AI/BI consumers connect as — output ports only (ADR-0013/M8),
# replacing the reuse of a domain owner role. It reads every port; PII is masked by the view.
ANALYTICS_READER = "analytics_reader"


def is_masked_port(owner: str, product: str) -> bool:
    """True if the port's output-port contract declares a maskingPolicy on any column (ADR-0013)."""
    for c in REPO_ROOT.glob(f"domains/{owner}/contracts/{product}.odcs.yaml"):
        defn = odcs.load(c)
        for obj in defn.get("schema") or []:
            if "output_port" not in (obj.get("tags") or []):
                continue
            for prop in obj.get("properties") or []:
                cps = prop.get("customProperties") or []
                if any(cp.get("property") == "maskingPolicy" for cp in cps):
                    return True
    return False


def _target(owner: str, product: str) -> tuple[str, str] | None:
    """Where consumers are granted: the masked view for a masked port, else the base port."""
    base = port_location(owner, product)
    if base is None:
        return None
    _, table = base
    return (f"{owner}_masked", table) if is_masked_port(owner, product) else base


def _grants() -> list[tuple[str, str, str]]:
    """Return (consumer_role, physical_schema, table) grants.

    Two consumption planes with different masking:
      * **End consumers** (BI/AI) connect as the least-privilege ``analytics_reader`` and read every
        port through the **masking view** where the port carries PII — they never see raw PII or the
        base table.
      * **Internal domain transformations** (another domain's dbt model consuming this port via a
        dbt-loom ref) read the **base** port relation — they are the trusted transformation plane,
        and any PII they republish must be re-governed by their own output port's contract.
    """
    out: list[tuple[str, str, str]] = []
    for path in _definition_files():
        defn = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        owner = (defn.get("owner") or {}).get("team")
        product = defn.get("name")
        base = port_location(owner, product) if owner and product else None
        if not base:
            continue
        reader_target = _target(owner, product) or base  # masked view if the port carries PII
        out.append((ANALYTICS_READER, *reader_target))  # end-consumer: masked
        # The owning domain's AI agent reads its own ports only (ADR-0021) — masked like any
        # end consumer — so agent isolation is enforced by the warehouse, not just the agent code.
        out.append((f"agent_{owner}", *reader_target))
        for consumer in defn.get("consumers") or []:
            team = (consumer or {}).get("team")
            if not team or team == owner:
                continue  # a domain already owns its own products
            out.append((f"{team}_role", *base))  # internal transformation: base relation
    return out


def _conninfo() -> str:
    """The admin plane connects as ``access_admin`` — never the superuser (X5).

    access_admin is a member of every domain role, so it can GRANT on the ports the domains own and
    own the ``<domain>_masked`` schemas; it holds no superuser or role-creation rights.
    """
    host = os.environ.get("ANALYTICS_HOST", "localhost")
    port = os.environ.get("ANALYTICS_INTERNAL_PORT", os.environ.get("ANALYTICS_PORT", "5433"))
    db = os.environ.get("ANALYTICS_DB", "analytics")
    pw = os.environ["ACCESS_ADMIN_PASSWORD"]
    return f"host={host} port={port} dbname={db} user=access_admin password={pw}"


def main() -> int:
    grants = _grants()
    if not grants:
        print("No consumer grants declared.")
        return 0
    # access_admin applies grants: as a member of each domain role it may GRANT on their objects.
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
