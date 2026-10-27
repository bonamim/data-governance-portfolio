"""Shared Airflow assets.

Domains react to producers and arrivals rather than to a clock (the asset-driven cross-DAG
trigger behind proof P4):

  olist-cache        the acquire DAG downloaded the Olist dataset (PoC source of everything)
  shop-oltp          the source simulator (re)seeded the operational database -> OLTP loads
  landing/<domain>   files arrived in the domain's landing inbox (ADR-0016): watched by an
                     event trigger on the arrival queue, not produced by any DAG
  raw/<domain>       the domain's raw tables were loaded -> its transform DAG
  published/<domain> its output ports were published -> consuming domains' transforms
"""

from __future__ import annotations

from airflow.sdk import Asset, AssetWatcher

OLIST_CACHE = Asset(name="olist-cache")
SHOP_OLTP = Asset(name="shop-oltp")


def landing_asset(domain: str, prefixes: list[str]) -> Asset:
    """Fired by arrivals under the domain's inbox prefixes (event-driven, windowed fan-in)."""
    from .arrival_trigger import LandingArrivalTrigger

    return Asset(
        name=f"landing/{domain}",
        watchers=[
            AssetWatcher(name=f"landing-arrivals-{domain}", trigger=LandingArrivalTrigger(prefixes))
        ],
    )


def raw_asset(domain: str) -> Asset:
    return Asset(name=f"raw/{domain}")


def published_asset(domain: str) -> Asset:
    """Produced when a domain's output ports are (re)published. Downstream domains
    whose aggregates consume them are scheduled on this."""
    return Asset(name=f"published/{domain}")
