"""Grant consumers exactly the output ports their data product definitions declare.

Federated governance, executed by the platform (data-mesh.md §4, the self-serve "Access" row): a
consumer named on a data product gets USAGE on the owning domain's output schema and SELECT on that
one port — nothing else. Consumers therefore read output ports, never raw or staging tables.

Run: ``make grants``. Idempotent: GRANT is additive and safe to re-run.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

import psycopg
from dgp_governance import products
from dgp_platform_ingestion.settings import PostgresSettings
from psycopg import sql

# The least-privilege role BI and AI consumers connect as: it reads every port, through the
# masking view where the port carries PII (ADR-0013, M8).
ANALYTICS_READER = "analytics_reader"
MASKED_SCHEMA_SUFFIX = "_masked"


@dataclass(frozen=True)
class Grant:
    role: str
    schema: str
    table: str

    @property
    def relation(self) -> str:
        return f"{self.schema}.{self.table}"


def is_masked(port: products.OutputPort | None) -> bool:
    """True if the port's contract declares a ``maskingPolicy`` on any column (ADR-0013)."""
    return bool(port and port.masking_policies())


def consumer_target(domain: str, product: str) -> tuple[str, str] | None:
    """Where end consumers read a port: its masking view if it carries PII, else the port itself."""
    base = products.location(domain, product)
    if base is None:
        return None
    if is_masked(products.output_port(product, domain)):
        return f"{domain}{MASKED_SCHEMA_SUFFIX}", product
    return base


def grants() -> list[Grant]:
    """Every grant the data product definitions imply.

    There are two ways to consume a port, masked differently:

    * **End consumers** (BI, AI) connect as ``analytics_reader``, and the owning domain's own agent
      as ``agent_<domain>`` (ADR-0021). They read through the masking view where the port carries
      PII, so they never see raw PII or the base table, and an agent's isolation is enforced by
      the warehouse, not only by the agent's code.
    * **Another domain's transformations** (its dbt models, through a dbt-loom ref) read the base
      relation as ``<team>_role``. That is the trusted transformation plane: any PII it
      republishes must be governed again by its own output port's contract.
    """
    out: list[Grant] = []
    for product in products.data_products():
        owner = product.owner
        if not owner:
            continue
        base = products.location(owner, product.name)
        if base is None:
            continue
        reader_target = consumer_target(owner, product.name) or base
        out.append(Grant(ANALYTICS_READER, *reader_target))
        out.append(Grant(f"agent_{owner}", *reader_target))
        for team in product.consumers:
            if team != owner:  # a domain already owns its own products
                out.append(Grant(f"{team}_role", *base))
    return out


def admin_conninfo() -> str:
    """The access admin plane connects as ``access_admin``, never the superuser (X5).

    ``access_admin`` is a member of every domain role, so it can GRANT on the ports the domains own
    and own the ``<domain>_masked`` schemas; it holds no superuser or role-creation rights.
    """
    return PostgresSettings.analytics(
        "access_admin", os.environ["ACCESS_ADMIN_PASSWORD"]
    ).conninfo()


def relation_exists(conn: psycopg.Connection, relation: str) -> bool:
    row = conn.execute("SELECT to_regclass(%s) IS NOT NULL", (relation,)).fetchone()
    return row is not None and row[0]


def main() -> int:
    planned = grants()
    if not planned:
        print("No consumer grants declared.")
        return 0
    applied = 0
    with psycopg.connect(admin_conninfo(), autocommit=False) as conn:
        for grant in planned:
            # A dbt rebuild drops table grants, so this runs again after every transform; a port
            # that isn't built yet is simply granted next time.
            if not relation_exists(conn, grant.relation):
                print(f"  skip {grant.relation} (not built yet)")
                continue
            role = sql.Identifier(grant.role)
            conn.execute(
                sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(grant.schema), role)
            )
            conn.execute(
                sql.SQL("GRANT SELECT ON {} TO {}").format(
                    sql.Identifier(grant.schema, grant.table), role
                )
            )
            print(f"  granted SELECT on {grant.relation} to {grant.role}")
            applied += 1
        conn.commit()
    print(f"Applied {applied} consumer grant(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
