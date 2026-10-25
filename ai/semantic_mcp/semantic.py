#!/usr/bin/env python3
"""Governed semantic computation — the engine behind the M7 AI consumer (ADR-0010, ADR-0020).

Two planes, deliberately:

* **Discovery is open.** ``list_data_products`` lists *every* data product in the mesh (from the
  catalog), flagging which are *governed* (have an Apache Ossie semantic model) and which are not,
  with the owning domain — so a user can explore freely and see what exists.
* **Computation is governed.** A metric value can only be obtained by compiling the domain's Ossie
  definition into SQL (via the shared ``platform/context/semantics/ossie.py`` reader — the same
  translation Superset uses), run against the warehouse as a **consumer role**. There is no
  free-form-SQL path: a name that is not a governed metric/dimension is refused, so ungoverned
  (raw/staging/PII) columns are unanswerable by construction — which is what keeps BI and AI on
  the same number.

The bridge between the planes is ``request_semantic_model``: when a user wants a metric on a product
that is not governed yet, that becomes an auditable **business request to the owning domain** (the
self-serve path), not a dead end. ``server.py`` wraps these as MCP tools;
``proofs/p10_ai_bi_agreement.py`` calls them directly (no LLM) to prove AI == Superset.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "platform" / "context" / "semantics"))
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "platform" / "core" / "contracts" / "src"))
import ossie  # noqa: E402  (shared Ossie->SQL reader — the one definition BI and AI both compile)
from dgp_platform_contracts import odcs  # noqa: E402  (the one contract reader: links inlined)

from governance.registry import column_tags, concept_of, domains  # noqa: E402  (owners, meaning)

# libpq URI for the warehouse, as a CONSUMER role (output ports only) — never an admin/owner role.
WAREHOUSE_URI = os.environ.get("SEMANTIC_WAREHOUSE_URI")
# A domain agent connects as its own role, granted only its domain's ports (ADR-0021, M8):
# SEMANTIC_WAREHOUSE_URI_<DOMAIN> (e.g. ..._SALES -> agent_sales). Falls back to the shared reader.
SCOPED_URI_ENV = "SEMANTIC_WAREHOUSE_URI_{}"
# Federation (ADR-0021): a domain agent is this same code scoped to one domain via
# DGP_SEMANTIC_DOMAIN — it then sees/answers ONLY that domain's governed products (isolation).
# Unset = platform-wide view, used by a single-server deploy and the orchestrator's in-proc router.
DOMAIN = os.environ.get("DGP_SEMANTIC_DOMAIN") or None
# Business requests to govern a product land here, for the owning domain to action (auditable, like
# governance/approvals/). Override in tests via SEMANTIC_REQUESTS_DIR.
REQUESTS_DIR = Path(
    os.environ.get("SEMANTIC_REQUESTS_DIR", str(REPO_ROOT / "governance" / "requests"))
)


def warehouse_uri() -> str | None:
    """The URI for the current scope: the domain agent's own role if configured, else the reader."""
    if DOMAIN:
        scoped = os.environ.get(SCOPED_URI_ENV.format(DOMAIN.upper()))
        if scoped:
            return scoped
    return WAREHOUSE_URI


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _audit(tool: str, decision: str, **fields: Any) -> None:
    """Structured action log — one JSON line per governed call (allowed or refused).

    Agentic-mesh governance expects every agent action to be traceable (who, which domain/agent,
    which tool, what policy decision, the exact SQL). We emit it to stderr (and to ``DGP_AUDIT_LOG``
    if set) so an agent's data access is auditable, not opaque.
    """
    record = {"ts": _now(), "agent": DOMAIN or "all-domains", "tool": tool, "decision": decision}
    record.update({k: v for k, v in fields.items() if v is not None})
    line = json.dumps(record, default=str)
    print(f"AUDIT {line}", file=sys.stderr)
    log = os.environ.get("DGP_AUDIT_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def _specs() -> list[tuple[Path, dict]]:
    return [(p, ossie.load_compiled(p)) for p in ossie.models(DOMAIN)]


def _governed_names() -> set[str]:
    """Product names that already have a semantic model (dataset name or source leaf)."""
    names: set[str] = set()
    for _, spec in _specs():
        for ds in ossie.datasets(spec):
            names.add(ds.get("name"))
            names.add(str(ds.get("source", "")).split(".")[-1])
    return {n for n in names if n}


def _owner_contact(team: str | None) -> str | None:
    for d in domains():
        if d.get("name") == team:
            return (d.get("owner") or {}).get("contact")
    return None


def _column_classes(domain: str | None, product: str | None) -> dict[str, str]:
    """{column: classification} for a product's output port, from its ODCS contract."""
    path = REPO_ROOT / "domains" / str(domain) / "contracts" / f"{product}.odcs.yaml"
    if not path.exists():
        return {}
    defn = odcs.load(path)
    return {
        p["name"]: p.get("classification", "unclassified")
        for obj in defn.get("schema") or []
        if "output_port" in (obj.get("tags") or [])
        for p in obj.get("properties") or []
        if p.get("name")
    }


def _column_privacy(domain: str | None, product: str | None) -> dict[str, str]:
    """{column: privacy category} for the output-port columns that hold personal data
    (identifier, quasi_identifier, sensitive — SNOWFLAKE.CORE.PRIVACY_CATEGORY), from the resolved
    contract; a column not listed is not personal data."""
    path = REPO_ROOT / "domains" / str(domain) / "contracts" / f"{product}.odcs.yaml"
    if not path.exists():
        return {}
    return {
        p["name"]: privacy
        for obj in odcs.load(path).get("schema") or []
        if "output_port" in (obj.get("tags") or [])
        for p in obj.get("properties") or []
        if p.get("name") and (privacy := column_tags(p).get("privacy_category"))
    }


def _column_concepts(domain: str | None, product: str | None) -> dict[str, str]:
    """{column: ontology concept} for a product's output port: columns of different products that
    share a concept mean the same thing (the join key), whatever they are called (ADR-0027)."""
    path = REPO_ROOT / "domains" / str(domain) / "contracts" / f"{product}.odcs.yaml"
    if not path.exists():
        return {}
    defn = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {
        p["name"]: concept
        for obj in defn.get("schema") or []
        if "output_port" in (obj.get("tags") or [])
        for p in obj.get("properties") or []
        if p.get("name") and (concept := concept_of(p, path))
    }


def _catalog_products() -> list[dict]:
    """Every data product in the mesh (from domains/*/data_products/*.yaml), with its owner and
    whether it is governed by a semantic model yet. This is the open discovery plane."""
    governed = _governed_names()
    out: list[dict] = []
    glob = f"domains/{DOMAIN}/data_products/*.yaml" if DOMAIN else "domains/*/data_products/*.yaml"
    for path in sorted(REPO_ROOT.glob(glob)):
        defn = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        name = defn.get("name")
        team = (defn.get("owner") or {}).get("team")
        out.append(
            {
                "data_product": name,
                "domain": team,
                "display_name": defn.get("display_name", name),
                "purpose": defn.get("purpose", ""),
                "owner_contact": _owner_contact(team),
                "governed": name in governed,
                "column_classification": _column_classes(team, name),
                "column_concepts": _column_concepts(team, name),
                "column_privacy": _column_privacy(team, name),
            }
        )
    return out


def _resolve(product: str) -> tuple[dict, dict]:
    """(spec, dataset) for a governed product; else a guided refusal toward the owning domain."""
    for _, spec in _specs():
        for ds in ossie.datasets(spec):
            if product in (ds.get("name"), spec.get("name")):
                return spec, ds
    known = next((p for p in _catalog_products() if p["data_product"] == product), None)
    if known:
        raise ValueError(
            f"'{product}' exists but has no governed metrics yet. It is owned by the "
            f"{known['domain']} domain ({known.get('owner_contact') or 'owner'}). Use "
            f"request_semantic_model('{product}', ...) to ask them to define its metrics."
        )
    raise ValueError(f"'{product}' is not a data product in the mesh (see list_data_products).")


def list_data_products() -> list[dict]:
    """Every data product in the mesh, flagged governed/ungoverned, with its owner, each column's
    classification and its ontology concept (columns sharing a concept mean the same thing); a
    governed product also lists its metrics and dimensions, so one call is enough to go straight
    to ``query_metric`` (discovery)."""
    out = _catalog_products()
    for product in out:
        if product["governed"]:
            listing = list_metrics(product["data_product"])
            product["metrics"] = [
                {"name": m["name"], "description": m["description"]} for m in listing["metrics"]
            ]
            product["dimensions"] = listing["dimensions"]
    return out


def list_metrics(product: str) -> dict:
    """The governed metrics and dimensions available for a data product (from its Ossie spec).

    An ungoverned product is a discovery answer, not an error: no metrics, plus its owner and how
    to request a model. Only an unknown (or, for a domain agent, foreign) product raises.
    """
    known = next((p for p in _catalog_products() if p["data_product"] == product), None)
    if known and not known["governed"]:
        return {
            "data_product": product,
            "governed": False,
            "domain": known["domain"],
            "owner_contact": known.get("owner_contact"),
            "metrics": [],
            "dimensions": [],
            "next_step": (
                f"No governed metrics yet. Use request_semantic_model('{product}', ...) to ask the "
                f"{known['domain']} domain ({known.get('owner_contact') or 'owner'}) to define "
                "them."
            ),
        }
    spec, ds = _resolve(product)
    return {
        "data_product": ds.get("name"),
        "governed": True,
        "source": ds.get("source"),
        "metrics": [
            {
                "name": m.get("name"),
                "description": m.get("description", ""),
                "expression": ossie.ansi_metric_expr(m),
            }
            for m in ossie.metrics(spec)
        ],
        "dimensions": [
            {"name": f.get("name"), "description": f.get("description", "")}
            for f in ossie.fields(ds)
        ],
    }


def _coerce(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if value is None or isinstance(value, (int, float, str, bool)):
        return value
    return str(value)


def query_metric(product: str, metrics: list[str], dimensions: list[str] = ()) -> dict:
    """Compute governed metric(s) for a data product, optionally by dimension.

    Governance: every requested metric must be a metric in the Ossie spec, and every dimension a
    field in it — anything else is refused (it is not in the semantic layer). The value is the
    governed ANSI SQL run as the consumer role; the SQL and definitions are returned for provenance.
    """
    spec, ds = _resolve(product)
    known_metrics = {m.get("name") for m in ossie.metrics(spec)}
    known_dims = {f.get("name") for f in ossie.fields(ds)}
    if not metrics:
        raise ValueError("ask for at least one governed metric")
    unknown_m = [m for m in metrics if m not in known_metrics]
    unknown_d = [d for d in dimensions if d not in known_dims]
    if unknown_m or unknown_d:
        _audit("query_metric", "refused", product=product, metrics=metrics, dimensions=dimensions)
        raise ValueError(
            "refused — not in the semantic layer: "
            f"metrics {unknown_m or '[]'}, dimensions {unknown_d or '[]'}. "
            f"Governed metrics: {sorted(known_metrics)}; dimensions: {sorted(known_dims)}."
        )

    sql = ossie.metric_sql(spec, ds, list(metrics), list(dimensions))
    uri = warehouse_uri()
    if not uri:
        raise RuntimeError("SEMANTIC_WAREHOUSE_URI is not set (the consumer-role warehouse URI).")
    _audit(
        "query_metric", "allowed", product=product, metrics=metrics, dimensions=dimensions, sql=sql
    )
    with psycopg.connect(uri) as conn, conn.cursor() as cur:
        cur.execute(sql)
        columns = [d.name for d in cur.description]
        rows = [{c: _coerce(v) for c, v in zip(columns, r, strict=True)} for r in cur.fetchall()]
    known = next((p for p in _catalog_products() if p["data_product"] == ds.get("name")), {})
    used = {
        c: cls
        for c, cls in (known.get("column_classification") or {}).items()
        if re.search(rf"\b{re.escape(c)}\b", sql)
    }
    return {
        "data_product": ds.get("name"),
        "owner_domain": known.get("domain"),
        "owner_contact": known.get("owner_contact"),
        # Sensitivity of every column the number was computed from, from the port's contract.
        "column_classification": used,
        "sql": sql,
        "columns": columns,
        "rows": rows,
        "definitions": [
            {"name": m.get("name"), "expression": ossie.ansi_metric_expr(m)}
            for m in ossie.metrics(spec)
            if m.get("name") in set(metrics)
        ],
    }


def _notify_owner(domain: str, record: dict, recorded: str) -> dict:
    """Tell the owning domain about a new request on its alert route (ADR-0016's webhook).

    The server holds the webhook URL (a secret, from the env var the registry names); the agent only
    learns whether the owner was notified, so it can't claim a notification that never went out.
    """
    route = next((d.get("alerts") or {} for d in domains() if d.get("name") == domain), {})
    channel, env = route.get("channel"), route.get("webhook_env")
    url = os.environ.get(env) if env else None
    if not url:
        return {
            "notified": False,
            "channel": channel,
            "reason": f"no webhook configured ({env or 'no alert route'} unset); the request is "
            "recorded but the owner has not been told",
        }
    body = {
        "text": f"[{domain}] New semantic model request for {record['data_product']}: "
        f"{record['business_need']} (requested by {record['requested_by']}; {recorded})",
        "domain": domain,
        "kind": "semantic_model_request",
        "channel": channel,
        "detail": {**record, "file": recorded},
    }
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 (configured route)
            ok = 200 <= resp.status < 300
    except OSError as exc:  # an unreachable route must not lose the request, which is on disk
        return {"notified": False, "channel": channel, "reason": f"webhook failed: {exc}"}
    return {"notified": ok, "channel": channel, **({} if ok else {"reason": "webhook rejected"})}


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")


def request_semantic_model(product: str, business_need: str, requested_by: str = "") -> dict:
    """Record a business request for the owning domain to define a semantic model for ``product``.

    This is the self-serve bridge from the open discovery plane to the governed one: exploration can
    surface a product that has no metrics yet, and instead of a dead end the user files an auditable
    request routed to the owner (like a breaking-change approval in governance/approvals/). The
    domain then authors the Ossie spec; nothing here fabricates ungoverned numbers.
    """
    known = next((p for p in _catalog_products() if p["data_product"] == product), None)
    if known is None:
        raise ValueError(f"'{product}' is not a data product in the mesh (see list_data_products).")
    if known["governed"]:
        raise ValueError(
            f"'{product}' is already governed — call list_metrics('{product}') and query it."
        )
    if not business_need.strip():
        raise ValueError("describe the business_need so the domain knows which metrics to define.")

    REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    record = {
        "data_product": product,
        "owner_domain": known["domain"],
        "owner_contact": known.get("owner_contact"),
        "business_need": business_need.strip(),
        "requested_by": requested_by or "unknown",
        "requested_at": now,
        "status": "open",
    }
    # One file per request: a second request on the same product must not overwrite an open one.
    stamp = now.replace("-", "").replace(":", "")
    path = REQUESTS_DIR / f"{known['domain']}.{_slug(product)}.{stamp}.yaml"
    n = 1
    while path.exists():
        n += 1
        path = REQUESTS_DIR / f"{known['domain']}.{_slug(product)}.{stamp}-{n}.yaml"
    path.write_text(
        yaml.safe_dump(record, sort_keys=False, allow_unicode=True, width=100), encoding="utf-8"
    )
    try:
        recorded = str(path.relative_to(REPO_ROOT))
    except ValueError:
        recorded = str(path)
    notification = _notify_owner(known["domain"], record, recorded)
    _audit(
        "request_semantic_model",
        "recorded",
        product=product,
        file=recorded,
        notified=notification["notified"],
        reason=notification.get("reason"),
    )
    return {
        "recorded": recorded,
        "routed_to": f"{known['domain']} ({known.get('owner_contact') or 'owner'})",
        "owner_notification": notification,
        "next": "the owning domain authors the Ossie semantic model; then the metric is governed.",
        **record,
    }
