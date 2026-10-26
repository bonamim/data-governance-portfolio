#!/usr/bin/env python3
"""Proof P11 — federated domain agents: isolation, deterministic routing, same number (ADR-0021).

The AI layer is a data mesh too: one governed agent *per domain*, and an orchestrator that routes a
question to the owning domain. This proves the three properties that make that governable:

  1. **Domain isolation** — a domain-scoped agent sees and answers ONLY its own products; another
     domain's product is refused (not merely ungoverned — invisible).
  2. **Deterministic routing** — the orchestrator resolves each product to its owning domain from
     the catalog (no LLM, no guess), and dispatches to that domain's registered agent.
  3. **Federation preserves the number** — a metric answered *through routing* equals the direct
     governed Ossie computation (so splitting into per-domain agents changes nothing a consumer
     sees; with P10, AI-via-orchestrator == Superset).
  4. **Isolation in the warehouse** (M8) — each agent's own role (``agent_<domain>``) can read its
     domain's ports and is DENIED every other domain's port, so isolation does not depend on the
     agent code alone. Runs when the per-domain URIs (``SEMANTIC_WAREHOUSE_URI_<DOMAIN>``) are set.

Isolation + routing need no warehouse (they run anywhere). The value check runs when
``SEMANTIC_WAREHOUSE_URI`` is set (the live platform, as for P10). Run: ``make prove-p11``.
"""

from __future__ import annotations

import os
import sys

import psycopg
from dgp_ai import agents as registry
from dgp_ai.router import Router
from dgp_ai.semantic import SCOPED_URI_ENV, SemanticLayer
from dgp_governance.repo import domain_of
from dgp_platform_access import apply_grants
from dgp_platform_semantics import ossie
from psycopg import errors, sql

URI = os.environ.get("SEMANTIC_WAREHOUSE_URI")
TOL = 1e-6


def _can_read(uri: str, schema: str, table: str) -> bool:
    with psycopg.connect(uri) as conn:
        try:
            conn.execute(sql.SQL("SELECT 1 FROM {} LIMIT 1").format(sql.Identifier(schema, table)))
            return True
        except errors.InsufficientPrivilege:
            return False


def _warehouse_value(query: str) -> float:
    with psycopg.connect(URI) as conn, conn.cursor() as cur:
        cur.execute(query)
        return float(cur.fetchone()[0])


def check_isolation(router: Router, agents: list[dict]) -> list[str]:
    """1. Each domain's agent sees only its own products; another domain's are invisible to it."""
    failures = []
    all_products = {p["data_product"]: p["domain"] for p in router.catalog()}
    for agent in agents:
        domain = agent["domain"]
        layer = SemanticLayer(domain)
        visible = {p["data_product"] for p in layer.list_data_products()}
        own = {p for p, d in all_products.items() if d == domain}
        if visible != own:
            failures.append(f"{agent['agent']}: sees {visible}, should see exactly {own}")
        foreign = next((p for p, d in all_products.items() if d != domain), None)
        if foreign:
            try:
                layer.list_metrics(foreign)
                failures.append(f"{agent['agent']}: answered foreign product '{foreign}'")
            except ValueError:
                pass  # correct: another domain's product is invisible to this agent
        print(f"  OK isolation: {agent['agent']} serves only {sorted(own)}")
    return failures


def check_routing(router: Router) -> list[str]:
    """2. Every product is routed to the domain that owns it (by where its model lives)."""
    failures = []
    for path in ossie.models():
        owner = domain_of(path)
        for ds in ossie.datasets(ossie.load(path)):
            routed = router.owning_domain(ds["name"])
            if routed != owner:
                failures.append(f"routing '{ds['name']}' -> '{routed}', should be '{owner}'")
            else:
                print(f"  OK routing: '{ds['name']}' -> {routed}")
    return failures


def check_values(router: Router) -> list[str]:
    """3. Federation preserves the number: a routed metric equals the direct Ossie SQL."""
    if not URI:
        print("  SKIP value check: set SEMANTIC_WAREHOUSE_URI to compare routed vs direct (P10).")
        return []
    failures = []
    for path in ossie.models():
        spec = ossie.load(path)
        for ds, metric in ((d, m) for d in ossie.datasets(spec) for m in ossie.metrics(spec)):
            name = metric["name"]
            routed = router.route_query(ds["name"], [name])
            via = float(routed["rows"][0][name])
            direct = _warehouse_value(ossie.metric_sql(spec, ds, [name]))
            agent = registry.agent_for_domain(routed["domain"])
            if abs(via - direct) > TOL:
                failures.append(f"{ds['name']}.{name}: routed {via} != direct {direct}")
            elif routed["routed_to"] != (agent or {}).get("agent"):
                failures.append(f"{ds['name']}.{name}: wrong routed_to {routed['routed_to']}")
            else:
                print(f"  OK value: {ds['name']}.{name} via {routed['routed_to']} = {via}")
    return failures


def check_warehouse_isolation(agents: list[dict]) -> list[str]:
    """4. In the warehouse, each agent's role reads its own ports and is denied the others."""
    failures = []
    grants = apply_grants.grants()
    all_ports = {(g.schema, g.table) for g in grants}
    for agent in agents:
        env = SCOPED_URI_ENV.format(agent["domain"].upper())
        uri = os.environ.get(env)
        if not uri:
            print(f"  SKIP db isolation: {env} not set")
            continue
        own = {(g.schema, g.table) for g in grants if g.role == agent["consumer_role"]}
        for port in sorted(all_ports):
            allowed = _can_read(uri, *port)
            if (port in own) != allowed:
                verb = "could read" if allowed else "was denied"
                failures.append(f"{agent['consumer_role']} {verb} {'.'.join(port)}")
        print(
            f"  OK db isolation: {agent['consumer_role']} reads "
            f"{sorted('.'.join(p) for p in own)}, denied {len(all_ports - own)} other port(s)"
        )
    return failures


def main() -> int:
    agents = registry.agents()
    if not agents:
        print("P11: no domain agents registered (no governed products yet).")
        return 0
    router = Router()
    failures = check_isolation(router, agents) + check_routing(router)
    failures += check_values(router) + check_warehouse_isolation(agents)
    if failures:
        print("\nP11 FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nP11 OK: isolation holds, routing is deterministic, federation preserves the number.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
