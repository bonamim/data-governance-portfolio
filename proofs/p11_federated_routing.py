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
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "ai" / "orchestrator"))
sys.path.insert(0, str(REPO / "ai" / "semantic-mcp"))
sys.path.insert(0, str(REPO / "platform" / "semantics"))
sys.path.insert(0, str(REPO / "platform" / "access"))
import grant_consumers  # noqa: E402  (the grants the warehouse enforces, from the definitions)
import ossie  # noqa: E402
import registry  # noqa: E402
import router  # noqa: E402
import semantic  # noqa: E402

URI = os.environ.get("SEMANTIC_WAREHOUSE_URI")
TOL = 1e-6


def _can_read(uri: str, relation: str) -> bool:
    import psycopg
    from psycopg import errors

    with psycopg.connect(uri) as conn:
        try:
            conn.execute(f"SELECT 1 FROM {relation} LIMIT 1")  # noqa: S608 (internal identifiers)
            return True
        except errors.InsufficientPrivilege:
            return False


def _warehouse_value(sql: str) -> float:
    import psycopg

    with psycopg.connect(URI) as conn, conn.cursor() as cur:
        cur.execute(sql)
        return float(cur.fetchone()[0])


def main() -> int:
    failures: list[str] = []
    agents = registry.agents()
    if not agents:
        print("P11: no domain agents registered (no governed products yet).")
        return 0

    # 1. Domain isolation — each agent sees only its own products; others are invisible.
    all_products = {p["data_product"]: p["domain"] for p in router.catalog()}
    for agent in agents:
        domain = agent["domain"]
        with router._scoped(domain):
            visible = {p["data_product"] for p in semantic.list_data_products()}
        own = {p for p, d in all_products.items() if d == domain}
        if visible != own:
            failures.append(f"{agent['agent']}: sees {visible}, should see exactly {own}")
        foreign = next((p for p, d in all_products.items() if d != domain), None)
        if foreign:
            with router._scoped(domain):
                try:
                    semantic.list_metrics(foreign)
                    failures.append(f"{agent['agent']}: answered foreign product '{foreign}'")
                except ValueError:
                    pass  # correct: another domain's product is invisible to this agent
        print(f"  OK isolation: {agent['agent']} serves only {sorted(own)}")

    # 2. Deterministic routing — every product resolves to the domain that owns it (by folder).
    for path in ossie.models():
        owner = ossie.domain_of(path)
        for ds in ossie.datasets(ossie.load(path)):
            product = ds["name"]
            routed = router.owning_domain(product)
            if routed != owner:
                failures.append(f"routing '{product}' -> '{routed}', should be '{owner}'")
            else:
                print(f"  OK routing: '{product}' -> {routed}")

    # 3. Federation preserves the number (needs the live warehouse).
    if not URI:
        print("  SKIP value check: set SEMANTIC_WAREHOUSE_URI to compare routed vs direct (P10).")
    else:
        for path in ossie.models():
            spec = ossie.load(path)
            for ds in ossie.datasets(spec):
                for metric in ossie.metrics(spec):
                    name = metric["name"]
                    routed = router.route_query(ds["name"], [name])
                    via = float(routed["rows"][0][name])
                    direct = _warehouse_value(ossie.metric_sql(spec, ds, [name]))
                    agent = registry.agent_for_domain(routed["domain"])
                    if abs(via - direct) > TOL:
                        failures.append(f"{ds['name']}.{name}: routed {via} != direct {direct}")
                    elif routed["routed_to"] != (agent or {}).get("agent"):
                        failures.append(
                            f"{ds['name']}.{name}: wrong routed_to {routed['routed_to']}"
                        )
                    else:
                        print(f"  OK value: {ds['name']}.{name} via {routed['routed_to']} = {via}")

    # 4. Isolation in the warehouse — each agent's role is denied other domains' ports.
    grants = grant_consumers._grants()
    all_ports = {f"{s}.{t}" for _, s, t in grants}
    for agent in agents:
        domain = agent["domain"]
        uri = os.environ.get(semantic.SCOPED_URI_ENV.format(domain.upper()))
        if not uri:
            print(f"  SKIP db isolation: {semantic.SCOPED_URI_ENV.format(domain.upper())} not set")
            continue
        own = {f"{s}.{t}" for r, s, t in grants if r == agent["consumer_role"]}
        for port in sorted(all_ports):
            allowed = _can_read(uri, port)
            if (port in own) != allowed:
                verb = "could read" if allowed else "was denied"
                failures.append(f"{agent['consumer_role']} {verb} {port}")
        print(
            f"  OK db isolation: {agent['consumer_role']} reads {sorted(own)}, "
            f"denied {len(all_ports - own)} other port(s)"
        )

    if failures:
        print("\nP11 FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nP11 OK: isolation holds, routing is deterministic, federation preserves the number.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
