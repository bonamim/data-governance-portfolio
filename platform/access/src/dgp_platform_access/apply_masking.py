"""Apply role-based dynamic PII masking as warehouse objects (ADR-0013, proof P8).

Governance is declared once in ODCS — a PII column carries a ``maskingPolicy`` (method +
``unmask_roles``) — and enforced per platform. Locally (Postgres) the enforcement is a **role-aware
masking view**: for every output port whose contract has a masked column, this creates
``<domain>_masked.<port>`` where each masked column is rendered

    CASE WHEN pg_has_role(current_user, '<unmask_role>', 'MEMBER') THEN <raw> ELSE <mask> END

``pg_has_role(current_user, …)`` is evaluated for the *invoking* session, and the view reads the
base table with its owner's rights (``access_admin``, a member of the domain roles). So consumers
granted only the view see the mask, an unmask role (e.g. compliance) sees the raw value, and
nobody reaches the base table through it. It is the local stand-in for a Snowflake masking policy
or a Databricks column mask (ADR-0013's per-platform table). Consumers are granted the view, never
the base table (see ``apply_grants``).

Run: ``make mask`` (re-run after a transform/publish, which rebuilds the base tables). Idempotent.
"""

from __future__ import annotations

import sys

import psycopg
from dgp_governance import products
from psycopg import sql

from .apply_grants import MASKED_SCHEMA_SUFFIX, admin_conninfo, relation_exists


def _mask_expr(column: str, method: str) -> sql.Composable:
    """The masked rendering of ``column`` for a consumer, per the policy method."""
    col = sql.Identifier(column)
    if method == "null":
        return sql.SQL("NULL")
    if method == "full":  # reveal nothing at all
        return sql.SQL("'***'")
    return sql.SQL("md5({})").format(col)  # "hash", and the safe default for anything unknown


def masking_plan() -> list[tuple[products.OutputPort, dict[str, dict]]]:
    """(port, {column: policy}) for every output port that masks a column. The contract is read
    resolved, so a column that inherits its ``maskingPolicy`` from a definition is masked too."""
    plan = [(port, port.masking_policies()) for port in products.output_ports().values()]
    return [(port, policies) for port, policies in plan if policies]


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
    with psycopg.connect(admin_conninfo(), autocommit=False) as conn:
        _ensure_roles(conn)
        for port, policies in masking_plan():
            location = port.location
            if location is None:
                print(f"  skip {port.contract.name} (no dbt model for {port.domain}.{port.name})")
                continue
            base_schema, table = location
            masked_schema = f"{port.domain}{MASKED_SCHEMA_SUFFIX}"
            if not relation_exists(conn, f"{base_schema}.{table}"):
                print(f"  skip {base_schema}.{table} (not built yet)")
                continue
            masked = sql.Identifier(masked_schema)
            conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(masked))
            conn.execute(
                sql.SQL("CREATE OR REPLACE VIEW {} AS SELECT {} FROM {}").format(
                    sql.Identifier(masked_schema, table),
                    _view_select([c["name"] for c in port.columns], policies),
                    sql.Identifier(base_schema, table),
                )
            )
            # The unmask role reads through the view (its CASE reveals the raw value). End
            # consumers are granted the view by apply_grants; nobody is granted the base table.
            conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO compliance").format(masked))
            conn.execute(
                sql.SQL("GRANT SELECT ON {} TO compliance").format(
                    sql.Identifier(masked_schema, table)
                )
            )
            print(
                f"  masked view {masked_schema}.{table} (role-aware: {', '.join(sorted(policies))})"
            )
            applied += 1
        conn.commit()
    print(f"Applied {applied} masking view(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
