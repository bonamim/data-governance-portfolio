"""Governed semantic computation — the engine behind the AI consumer (ADR-0010, ADR-0020).

It works on two planes, on purpose:

* **Discovery is open.** ``list_data_products`` lists every data product in the mesh, says which
  are *governed* (they have an Apache Ossie semantic model) and which are not, and names the
  owning domain — so a user can explore freely and see what exists.
* **Computation is governed.** A metric value comes only from compiling the domain's Ossie
  definition into SQL (``dgp_platform_semantics.ossie``, the same translation Superset uses) and
  running it against the warehouse as a **consumer role**. There is no free-form SQL: a name that
  is not a governed metric or dimension is refused, so raw, staging and PII columns can't be
  answered at all — which is what keeps BI and AI on the same number.

``request_semantic_model`` bridges the two. When someone wants a metric on a product that isn't
governed yet, it becomes an auditable request to the owning domain — the self-serve path — rather
than a dead end.

A ``SemanticLayer`` is scoped either to the whole platform or to one domain. A domain's agent
(ADR-0021) is the same code scoped to its domain: it sees and answers only that domain's products,
and connects as that domain's own warehouse role. ``semantic_server`` exposes one layer as MCP
tools; the ``router`` holds one per domain; proof P10 calls a layer directly (no LLM) to show that
AI and Superset agree.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, LiteralString, cast

import psycopg
import yaml
from dgp_governance import products, registry
from dgp_governance.repo import relative, root
from dgp_platform_semantics import ossie

# The consumer-role URI of the warehouse (output ports only) — never an admin or owner role.
WAREHOUSE_URI_ENV = "SEMANTIC_WAREHOUSE_URI"
# A domain's agent connects as its own role, granted only that domain's ports (ADR-0021, M8):
# SEMANTIC_WAREHOUSE_URI_<DOMAIN>, e.g. ..._SALES for agent_sales. Unset: the shared reader.
SCOPED_URI_ENV = "SEMANTIC_WAREHOUSE_URI_{}"
DOMAIN_ENV = "DGP_SEMANTIC_DOMAIN"
REQUESTS_DIR_ENV = "SEMANTIC_REQUESTS_DIR"
AUDIT_LOG_ENV = "DGP_AUDIT_LOG"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def audit(agent: str, tool: str, decision: str, **fields: Any) -> None:
    """Log one governed call, allowed or refused, as a JSON line.

    Every agent action should be traceable: which agent, which tool, what the policy decided and
    the exact SQL. The line goes to stderr, and to the file ``DGP_AUDIT_LOG`` names if set.
    """
    record = {"ts": _now(), "agent": agent, "tool": tool, "decision": decision}
    record.update({k: v for k, v in fields.items() if v is not None})
    line = json.dumps(record, default=str)
    print(f"AUDIT {line}", file=sys.stderr)
    if log := os.environ.get(AUDIT_LOG_ENV):
        with Path(log).open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def column_classification(port: products.OutputPort) -> dict[str, str]:
    """Column -> its classification, as the resolved contract has it."""
    return {c["name"]: c.get("classification", "unclassified") for c in port.columns}


def column_privacy(port: products.OutputPort) -> dict[str, str]:
    """Column -> privacy category, for the columns that hold personal data (identifier,
    quasi_identifier, sensitive — SNOWFLAKE.CORE.PRIVACY_CATEGORY). A column not listed is not
    personal data."""
    out = {}
    for column in port.columns:
        if privacy := registry.column_tags(column).get("privacy_category"):
            out[column["name"]] = privacy
    return out


def column_concepts(port: products.OutputPort) -> dict[str, str]:
    """Column -> its ontology concept. Columns of different products that share a concept mean the
    same thing — the join key — whatever they are called (ADR-0027)."""
    out = {}
    for column in port.columns:
        if concept := registry.concept_of(column, port.contract):
            out[column["name"]] = concept
    return out


def _coerce(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if value is None or isinstance(value, (int, float, str, bool)):
        return value
    return str(value)


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")


@dataclass(frozen=True)
class SemanticLayer:
    """The governed semantic layer as one agent sees it: every domain, or only ``domain``'s."""

    domain: str | None = None
    requests_dir: Path | None = None  # where requests are filed; default governance/requests/

    @classmethod
    def from_env(cls) -> SemanticLayer:
        """Scoped by ``DGP_SEMANTIC_DOMAIN`` (unset: the platform-wide view)."""
        requests = os.environ.get(REQUESTS_DIR_ENV)
        return cls(os.environ.get(DOMAIN_ENV) or None, Path(requests) if requests else None)

    @property
    def agent(self) -> str:
        """The agent's name, which says its scope (``dgp-semantic-sales`` or ``dgp-semantic``)."""
        return f"dgp-semantic-{self.domain}" if self.domain else "dgp-semantic"

    def warehouse_uri(self) -> str | None:
        """The domain agent's own role if one is configured, else the shared consumer role."""
        if self.domain and (scoped := os.environ.get(SCOPED_URI_ENV.format(self.domain.upper()))):
            return scoped
        return os.environ.get(WAREHOUSE_URI_ENV)

    # Discovery (open).

    def list_data_products(self) -> list[dict]:
        """Every data product in scope, flagged governed or not, with its owner, each column's
        classification, privacy category and ontology concept. A governed product also lists its
        metrics and dimensions, so one call is enough to go straight to ``query_metric``."""
        out = []
        for entry in self._catalog():
            if entry["governed"]:
                listing = self.list_metrics(entry["data_product"])
                entry["metrics"] = [
                    {"name": m["name"], "description": m["description"]} for m in listing["metrics"]
                ]
                entry["dimensions"] = listing["dimensions"]
            out.append(entry)
        return out

    def list_metrics(self, product: str) -> dict:
        """The governed metrics and dimensions of a data product (from its Ossie model).

        An ungoverned product is a discovery answer, not an error: no metrics, plus its owner and
        how to ask for a model. Only an unknown product (or, for a domain agent, another domain's
        product) raises.
        """
        known = self._entry(product)
        if known and not known["governed"]:
            return {
                "data_product": product,
                "governed": False,
                "domain": known["domain"],
                "owner_contact": known.get("owner_contact"),
                "metrics": [],
                "dimensions": [],
                "next_step": (
                    f"No governed metrics yet. Use request_semantic_model('{product}', ...) to ask "
                    f"the {known['domain']} domain ({known.get('owner_contact') or 'owner'}) to "
                    "define them."
                ),
            }
        spec, dataset = self._resolve(product)
        return {
            "data_product": dataset.get("name"),
            "governed": True,
            "source": dataset.get("source"),
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
                for f in ossie.fields(dataset)
            ],
        }

    # Computation (governed).

    def query_metric(
        self, product: str, metrics: Sequence[str], dimensions: Sequence[str] = ()
    ) -> dict:
        """Compute governed metrics for a data product, optionally by dimension.

        Every requested metric must be a metric of the product's Ossie model and every dimension
        one of its fields; anything else is refused, because it is not in the semantic layer. The
        value is the governed ANSI SQL run as the consumer role, and the SQL and the definitions
        come back with it for provenance.
        """
        spec, dataset = self._resolve(product)
        known_metrics = {m["name"] for m in ossie.metrics(spec) if m.get("name")}
        known_dims = {f["name"] for f in ossie.fields(dataset) if f.get("name")}
        if not metrics:
            raise ValueError("ask for at least one governed metric")
        unknown_m = [m for m in metrics if m not in known_metrics]
        unknown_d = [d for d in dimensions if d not in known_dims]
        asked = {"product": product, "metrics": list(metrics), "dimensions": list(dimensions)}
        if unknown_m or unknown_d:
            audit(self.agent, "query_metric", "refused", **asked)
            raise ValueError(
                "refused — not in the semantic layer: "
                f"metrics {unknown_m or '[]'}, dimensions {unknown_d or '[]'}. "
                f"Governed metrics: {sorted(known_metrics)}; dimensions: {sorted(known_dims)}."
            )

        sql = ossie.metric_sql(spec, dataset, metrics, dimensions)
        uri = self.warehouse_uri()
        if not uri:
            raise RuntimeError(f"{WAREHOUSE_URI_ENV} is not set (the consumer-role warehouse URI).")
        audit(self.agent, "query_metric", "allowed", **asked, sql=sql)
        with psycopg.connect(uri) as conn, conn.cursor() as cur:
            cur.execute(cast(LiteralString, sql))  # built only from governed names (ADR-0010)
            columns = [d.name for d in cur.description or []]
            rows = [
                {c: _coerce(v) for c, v in zip(columns, row, strict=True)} for row in cur.fetchall()
            ]
        known = self._entry(dataset.get("name")) or {}
        used = {
            column: cls
            for column, cls in (known.get("column_classification") or {}).items()
            if re.search(rf"\b{re.escape(column)}\b", sql)
        }
        return {
            "data_product": dataset.get("name"),
            "owner_domain": known.get("domain"),
            "owner_contact": known.get("owner_contact"),
            # How sensitive is every column the number was computed from (the port's contract).
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

    # The bridge: a business request to the owning domain.

    def request_semantic_model(
        self, product: str, business_need: str, requested_by: str = ""
    ) -> dict:
        """File a business request for the owning domain to define a semantic model for
        ``product``, and tell the owner.

        This is the self-serve bridge from the open plane to the governed one: exploring can turn
        up a product with no metrics yet, and instead of a dead end the user files an auditable
        request routed to its owner (like a breaking-change approval in governance/approvals/).
        The domain then writes the Ossie model; nothing here makes up an ungoverned number.
        """
        known = self._entry(product)
        if known is None:
            raise ValueError(
                f"'{product}' is not a data product in the mesh (see list_data_products)."
            )
        if known["governed"]:
            raise ValueError(
                f"'{product}' is already governed — call list_metrics('{product}') and query it."
            )
        if not business_need.strip():
            raise ValueError(
                "describe the business_need so the domain knows which metrics to define."
            )

        now = _now()
        record = {
            "data_product": product,
            "owner_domain": known["domain"],
            "owner_contact": known.get("owner_contact"),
            "business_need": business_need.strip(),
            "requested_by": requested_by or "unknown",
            "requested_at": now,
            "status": "open",
        }
        path = self._new_request_file(known["domain"], product, now)
        path.write_text(
            yaml.safe_dump(record, sort_keys=False, allow_unicode=True, width=100), encoding="utf-8"
        )
        recorded = relative(path)
        notification = _notify_owner(known["domain"], record, recorded)
        audit(
            self.agent,
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
            "next": "the owning domain writes the Ossie semantic model; then it is governed.",
            **record,
        }

    # Internals.

    def _models(self) -> list[dict]:
        """The compiled semantic models in scope."""
        return [ossie.load_compiled(p) for p in ossie.models(self.domain)]

    def _resolve(self, product: str) -> tuple[dict, dict]:
        """(model, dataset) for a governed product; otherwise a refusal that points to the owner."""
        for spec in self._models():
            for dataset in ossie.datasets(spec):
                if product in (dataset.get("name"), spec.get("name")):
                    return spec, dataset
        known = self._entry(product)
        if known:
            raise ValueError(
                f"'{product}' exists but has no governed metrics yet. It is owned by the "
                f"{known['domain']} domain ({known.get('owner_contact') or 'owner'}). Use "
                f"request_semantic_model('{product}', ...) to ask them to define its metrics."
            )
        raise ValueError(f"'{product}' is not a data product in the mesh (see list_data_products).")

    def _governed_names(self) -> set[str]:
        """Products that have a semantic model, by dataset name or source table."""
        names = {
            name
            for spec in self._models()
            for dataset in ossie.datasets(spec)
            for name in (dataset.get("name"), ossie.source_table(dataset))
        }
        return {n for n in names if n}

    def _catalog(self, only: str | None = None) -> list[dict]:
        """The discovery entries of the data products in scope (or just the one named ``only``)."""
        governed = self._governed_names()
        contacts = {
            d.get("name"): (d.get("owner") or {}).get("contact") for d in registry.domains()
        }
        out = []
        for product in products.data_products(self.domain):
            if only is not None and product.name != only:
                continue
            port = products.output_port(product.name, product.owner) if product.owner else None
            out.append(
                {
                    "data_product": product.name,
                    "domain": product.owner,
                    "display_name": product.definition.get("display_name", product.name),
                    "purpose": product.definition.get("purpose", ""),
                    "owner_contact": contacts.get(product.owner),
                    "governed": product.name in governed,
                    "column_classification": column_classification(port) if port else {},
                    "column_concepts": column_concepts(port) if port else {},
                    "column_privacy": column_privacy(port) if port else {},
                }
            )
        return out

    def _entry(self, product: str | None) -> dict | None:
        return next(iter(self._catalog(only=product)), None) if product else None

    def _new_request_file(self, domain: str, product: str, now: str) -> Path:
        """A new file per request: a second request on a product never overwrites the first."""
        folder = self.requests_dir or root() / "governance" / "requests"
        folder.mkdir(parents=True, exist_ok=True)
        stem = f"{domain}.{_slug(product)}.{now.replace('-', '').replace(':', '')}"
        path, n = folder / f"{stem}.yaml", 1
        while path.exists():
            n += 1
            path = folder / f"{stem}-{n}.yaml"
        return path


def _notify_owner(domain: str, record: dict, recorded: str) -> dict:
    """Tell the owning domain about a new request, on its alert route (ADR-0016's webhook).

    The server holds the webhook URL (a secret, in the env var the registry names). The agent only
    learns whether the owner was told, so it can't claim a notification that never went out.
    """
    route = (registry.domain(domain) or {}).get("alerts") or {}
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
    req = urllib.request.Request(  # noqa: S310 — the domain's configured alert route
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 — the domain's configured route
            ok = 200 <= resp.status < 300
    except OSError as exc:  # an unreachable route must not lose the request, which is on disk
        return {"notified": False, "channel": channel, "reason": f"webhook failed: {exc}"}
    return {"notified": ok, "channel": channel, **({} if ok else {"reason": "webhook rejected"})}
