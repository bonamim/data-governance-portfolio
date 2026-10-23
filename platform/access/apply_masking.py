#!/usr/bin/env python3
"""Apply role-based dynamic PII masking as warehouse objects (ADR-0013, proof P8).

Governance is declared once in ODCS — a PII column carries a ``maskingPolicy`` (method +
``unmask_roles``) — and enforced per platform. Locally (Postgres) the enforcement is a **role-aware
masking view**: for every output port whose contract has a masked column, this creates
``<domain>_masked.<port>`` where each masked column is rendered

    CASE WHEN pg_has_role(current_user, '<unmask_role>', 'MEMBER') THEN <raw> ELSE <mask> END

``pg_has_role(current_user, …)`` is evaluated for the *invoking* session, and the view reads the
base table with its (superuser) owner's rights — so consumers granted only the view see the mask, an
unmask role (e.g. compliance) sees the raw value, and nobody reaches the base table through it. It
is the faithful local stand-in for a Snowflake masking policy / Databricks column mask (ADR-0013's
per-platform table). Consumers are granted the view, never the base table (see grant_consumers.py).

Run: ``make mask`` (re-run after a transform/publish, which rebuilds the base tables). Idempotent.
"""

from __future__ import annotations

import sys
from pathlib import Path

import psycopg
from dgp_platform_quality import odcs
from psycopg import sql

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "platform" / "access"))
from grant_consumers import _conninfo, port_location  # noqa: E402 (reuse the access helpers)

MASK_SCHEMA_SUFFIX = "_masked"


def _mask_expr(column: str, method: str) -> sql.Composable:
    """The masked rendering of ``column`` for a consumer, per the policy method."""
    col = sql.Identifier(column)
    if method == "hash":
        return sql.SQL("md5({})").format(col)
    if method == "null":
        return sql.SQL("NULL")
    # full = reveal nothing but the type's empty value; default to hash for anything unknown.
    if method == "full":
        return sql.SQL("'***'")
    return sql.SQL("md5({})").format(col)


def _port_contracts() -> list[Path]:
    return sorted(REPO_ROOT.glob("domains/*/contracts/*.odcs.yaml"))


def masking_plan() -> list[tuple[Path, dict, dict[str, dict]]]:
    """(contract, resolved contract, {column: policy}) for every output port that masks a column.
    Read resolved, so a column that inherits its maskingPolicy from a definition is masked too."""
    out = []
    for contract in _port_contracts():
        defn = odcs.load(contract)
        if policies := _masked_columns(defn):
            out.append((contract, defn, policies))
    return out


def _masked_columns(defn: dict) -> dict[str, dict]:
    """{column: policy} for every PII column of the output port that declares a maskingPolicy."""
    out: dict[str, dict] = {}
    for obj in defn.get("schema") or []:
        if "output_port" not in (obj.get("tags") or []):
            continue
        for prop in obj.get("properties") or []:
            for cp in prop.get("customProperties") or []:
                if cp.get("property") == "maskingPolicy" and prop.get("name"):
                    out[prop["name"]] = cp.get("value") or {}
    return out


def _columns(defn: dict) -> list[str]:
    for obj in defn.get("schema") or []:
        if "output_port" in (obj.get("tags") or []):
            return [p["name"] for p in obj.get("properties") or [] if p.get("name")]
    return []


def _view_select(columns: list[str], policies: dict[str, dict]) -> sql.Composable:
    parts: list[sql.Composable] = []
    for c in columns:
        if c in policies:
            roles = policies[c].get("unmask_roles") or []
            method = policies[c].get("method", "hash")
            checks = sql.SQL(" OR ").join(
                sql.SQL("pg_has_role(current_user, {}, 'MEMBER')").format(sql.Literal(r))
                for r in roles
            ) or sql.SQL("false")
            parts.append(
                sql.SQL("CASE WHEN {checks} THEN {raw} ELSE {masked} END AS {alias}").format(
                    checks=checks,
                    raw=sql.Identifier(c),
                    masked=_mask_expr(c, method),
                    alias=sql.Identifier(c),
                )
            )
        else:
            parts.append(sql.Identifier(c))
    return sql.SQL(", ").join(parts)


def _ensure_roles(conn: psycopg.Connection) -> None:
    """Fail clearly if the consumer/unmask roles are missing.

    Roles are infrastructure: the init scripts create them (``make db-roles`` on a running
    warehouse). The admin plane runs as access_admin, which deliberately cannot create roles (X5).
    compliance is LOGIN locally only — in production it is a NOLOGIN group GRANTed to the users
    allowed to unmask.
    """
    for role in ("analytics_reader", "compliance"):
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
            raise SystemExit(f"role {role} is missing — run `make db-roles`.")


def main() -> int:
    applied = 0
    with psycopg.connect(_conninfo(), autocommit=False) as conn:
        _ensure_roles(conn)
        for contract, defn, policies in masking_plan():
            owner = (defn.get("team") or {}).get("name") or defn.get("domain")
            product = contract.stem.split(".")[0]
            location = port_location(owner, product) if owner else None
            if not location:
                print(f"  skip {contract.name} (no built model for {owner}.{product})")
                continue
            base_schema, table = location
            masked_schema = f"{owner}{MASK_SCHEMA_SUFFIX}"
            if not conn.execute(
                "SELECT to_regclass(%s) IS NOT NULL", (f"{base_schema}.{table}",)
            ).fetchone()[0]:
                print(f"  skip {base_schema}.{table} (not built yet)")
                continue
            conn.execute(
                sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(masked_schema))
            )
            conn.execute(
                sql.SQL("CREATE OR REPLACE VIEW {}.{} AS SELECT {} FROM {}.{}").format(
                    sql.Identifier(masked_schema),
                    sql.Identifier(table),
                    _view_select(_columns(defn), policies),
                    sql.Identifier(base_schema),
                    sql.Identifier(table),
                )
            )
            # The unmask role reads through the view (its CASE reveals the raw value). End consumers
            # are granted the view by grant_consumers (masked-aware). Never the base table.
            conn.execute(
                sql.SQL("GRANT USAGE ON SCHEMA {} TO compliance").format(
                    sql.Identifier(masked_schema)
                )
            )
            conn.execute(
                sql.SQL("GRANT SELECT ON {}.{} TO compliance").format(
                    sql.Identifier(masked_schema), sql.Identifier(table)
                )
            )
            cols = ", ".join(sorted(policies))
            print(f"  masked view {masked_schema}.{table} (role-aware columns: {cols})")
            applied += 1
        conn.commit()
    print(f"Applied {applied} masking view(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
