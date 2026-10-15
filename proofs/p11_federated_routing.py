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
import ossie  # noqa: E402
import registry  # noqa: E402
import router  # noqa: E402
import semantic  # noqa: E402

URI = os.environ.get("SEMANTIC_WAREHOUSE_URI")
TOL = 1e-6


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

    if failures:
        print("\nP11 FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nP11 OK: isolation holds, routing is deterministic, federation preserves the number.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
