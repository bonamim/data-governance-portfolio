#!/usr/bin/env python3
"""Push the *governance* layer into DataHub from our source-of-truth definitions.

Technical metadata (datasets, lineage, tests) comes from the dbt/Postgres ingestion recipes.
This adds the mesh governance on top, generated — not hand-maintained in the UI — from the same
files that drive grants and the marketplace:

  * **Domains** — one per mesh domain; every warehouse/dbt dataset is filed under its domain.
  * **Data products** — one per ``domains/*/data_products/*.yaml``: display name, purpose,
    owner, and the output-port datasets (Postgres + dbt sibling) as its assets.

Run via ``make catalog-govern`` (isolated DataHub SDK env). Idempotent: re-running upserts.
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

import yaml
from datahub.api.entities.dataproduct.dataproduct import DataProduct
from datahub.emitter.mce_builder import make_dataset_urn, make_domain_urn, make_group_urn
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.rest_emitter import DatahubRestEmitter
from datahub.metadata.schema_classes import DomainPropertiesClass, DomainsClass

REPO_ROOT = Path(__file__).resolve().parents[2]
GMS = os.environ.get("DATAHUB_GMS", "http://localhost:8080")
DB = os.environ.get("ANALYTICS_DB", "analytics")
# Mesh domains (directories under domains/ plus the top-level reference domain).
DOMAINS = ["customer", "customer_experience", "marketplace", "sales", "reference"]


def _pretty(domain: str) -> str:
    return domain.replace("_", " ").title()


def _domain_of(schema: str) -> str | None:
    """Map a warehouse schema (<domain>_<layer>) to its mesh domain (longest match wins)."""
    for d in sorted(DOMAINS, key=len, reverse=True):
        if schema == d or schema.startswith(d + "_"):
            return d
    return None


def _search_datasets(platform: str) -> list[str]:
    body = json.dumps(
        {
            "input": "*",
            "entity": "dataset",
            "start": 0,
            "count": 500,
            "filter": {
                "or": [{"and": [{"field": "platform", "value": f"urn:li:dataPlatform:{platform}"}]}]
            },
        }
    ).encode()
    req = urllib.request.Request(
        f"{GMS}/entities?action=search", data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req) as resp:  # noqa: S310 (local GMS)
        return [e["entity"] for e in json.load(resp)["value"]["entities"]]


def _all_dataset_urns() -> list[str]:
    # Query per platform: an unfiltered search collapses dbt/Postgres siblings and returns only
    # one of each pair, so we would miss half the datasets for domain filing.
    return _search_datasets("postgres") + _search_datasets("dbt")


def _schema_from_urn(urn: str) -> str | None:
    # urn:li:dataset:(urn:li:dataPlatform:<p>,<db>.<schema>.<table>,PROD)
    try:
        name = urn.split(",")[1]  # <db>.<schema>.<table>
        return name.split(".")[1]
    except IndexError:
        return None


def _port_location(owner: str, product: str) -> tuple[str, str] | None:
    """Physical (schema, table) of an output port, from its dbt model's layer folder."""
    for path in (REPO_ROOT / "domains" / owner / "dbt" / "models").rglob(f"{product}.sql"):
        return f"{owner}_{path.parent.name}", product
    return None


def main() -> int:
    emitter = DatahubRestEmitter(gms_server=GMS)

    # 1. Domains
    for d in DOMAINS:
        emitter.emit(
            MetadataChangeProposalWrapper(
                entityUrn=make_domain_urn(d),
                aspect=DomainPropertiesClass(name=_pretty(d), description=f"{_pretty(d)} domain."),
            )
        )
    print(f"  domains: {', '.join(DOMAINS)}")

    # 2. File every dataset under its domain
    filed = 0
    for urn in _all_dataset_urns():
        schema = _schema_from_urn(urn)
        domain = _domain_of(schema) if schema else None
        if not domain:
            continue
        emitter.emit(
            MetadataChangeProposalWrapper(
                entityUrn=urn, aspect=DomainsClass(domains=[make_domain_urn(domain)])
            )
        )
        filed += 1
    print(f"  datasets filed under a domain: {filed}")

    # 3. Data products from the definitions
    for path in REPO_ROOT.glob("domains/*/data_products/*.yaml"):
        defn = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        owner = (defn.get("owner") or {}).get("team")
        name = defn.get("name")
        loc = _port_location(owner, name) if owner and name else None
        if not loc:
            continue
        schema, table = loc
        assets = [
            make_dataset_urn("postgres", f"{DB}.{schema}.{table}", "PROD"),
            make_dataset_urn("dbt", f"{DB}.{schema}.{table}", "PROD"),
        ]
        dp = DataProduct(
            id=f"{owner}.{name}",
            display_name=defn.get("display_name", name),
            domain=make_domain_urn(owner),
            description=defn.get("purpose"),
            assets=assets,
            owners=[{"id": make_group_urn(owner), "type": "BUSINESS_OWNER"}],
        )
        for mcp in dp.generate_mcp(upsert=True):
            emitter.emit(mcp)
        # Put the product in its domain explicitly (the high-level domain= does not emit it).
        emitter.emit(
            MetadataChangeProposalWrapper(
                entityUrn=f"urn:li:dataProduct:{owner}.{name}",
                aspect=DomainsClass(domains=[make_domain_urn(owner)]),
            )
        )
        print(f"  data product: {owner}.{name} -> {schema}.{table}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
