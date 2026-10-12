"""Shared Airflow assets.

The acquire DAG produces OLIST_CACHE; each domain's ingestion DAG is scheduled on
it, so domains react to the producer rather than to a clock (the asset-driven
cross-DAG trigger behind proof P4). Each domain also produces its own raw asset,
which later milestones' transform DAGs will consume.
"""

from __future__ import annotations

from airflow.sdk import Asset

OLIST_CACHE = Asset(name="olist-cache")


def raw_asset(domain: str) -> Asset:
    return Asset(name=f"raw/{domain}")
