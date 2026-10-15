"""Airflow 3 DAG factories for ingestion.

- ``build_acquire_dag()`` — one platform DAG that downloads the Olist dataset and
  publishes the OLIST_CACHE asset.
- ``build_ingestion_dag(domain)`` — one DAG per domain, scheduled on OLIST_CACHE,
  that seeds / lands / loads only that domain's datasets and publishes the domain's
  raw asset. A domain's DAG file is a single call to this factory, so domains write
  no orchestration code (ADR-0008); isolation between domains is proof P1.

The ``seed`` (populate shop_oltp) and ``land`` (a partner delivering into the landing inbox) tasks
are local PoC *simulators* of the source systems: in production the operational DB and the landing
zone are fed by real producers, leaving only ``load_raw`` (source -> <domain>_raw), which is the
Airflow node that carries lineage into the catalog.
"""

from __future__ import annotations

import os

import pendulum
from airflow.sdk import dag, get_current_context, task
from dgp_platform_ingestion import layout, pipeline
from dgp_platform_ingestion.config import load_datasets

from . import runtime
from .assets import OLIST_CACHE, raw_asset

_START = pendulum.datetime(2026, 1, 1, tz="UTC")
_DEFAULT_ARGS = {"retries": 2, "retry_delay": pendulum.duration(minutes=1)}
_DB = os.environ.get("ANALYTICS_DB", "analytics")
_ARCHIVE = os.environ.get("LANDING_ARCHIVE_BUCKET", "landing-archive")


def _dataset(platform: str, name: str):
    """A DataHub lineage entity, matching the ingestion URNs so Airflow lineage joins the
    dbt/Postgres graph. Lazy-imported so the factory still works without the plugin (tests)."""
    try:
        from datahub_airflow_plugin.entities import Dataset
    except ImportError:
        return None
    return Dataset(platform=platform, name=name, env="PROD")


def _raw_outlets(datasets) -> list:
    """The <domain>_raw tables this load produces (matches the warehouse ingestion URN)."""
    out = []
    for d in datasets:
        if ds := _dataset("postgres", f"{_DB}.{d.raw_schema}.{d.raw_table}"):
            out.append(ds)
    return out


def _source_inlets(datasets) -> list:
    """The upstream sources: the shop_oltp table (OLTP) or the landing dataset (file datasets) —
    the same S3 entity ``make catalog-quality`` publishes with the gate's assertions (ADR-0024)."""
    out = []
    for d in datasets:
        if d.source_type == "oltp":
            ds = _dataset("postgres", f"shop_oltp.shop.{d.oltp_table}")
        else:
            ds = _dataset("s3", layout.catalog_name(_ARCHIVE, d.source_system, d.name))
        if ds:
            out.append(ds)
    return out


def build_acquire_dag():
    @dag(
        dag_id="platform_acquire_olist",
        schedule="@daily",
        start_date=_START,
        catchup=False,
        default_args=_DEFAULT_ARGS,
        tags=["platform", "ingestion"],
        doc_md="Download the Olist dataset from Kaggle and publish the OLIST_CACHE asset.",
    )
    def _dag():
        @task(outlets=[OLIST_CACHE])
        def acquire():
            pipeline.acquire(runtime.repo_root(), runtime.settings(), runtime.data_dir())

        acquire()

    return _dag()


def build_ingestion_dag(domain: str):
    # Read at parse time (repo is mounted) to decide which steps this domain needs.
    datasets = load_datasets(runtime.repo_root(), domain)
    has_oltp = any(d.source_type == "oltp" for d in datasets)
    has_landing = any(d.source_type == "landing" for d in datasets)

    @dag(
        dag_id=f"{domain}_ingestion",
        schedule=[OLIST_CACHE],
        start_date=_START,
        catchup=False,
        default_args=_DEFAULT_ARGS,
        tags=[domain, "ingestion"],
        doc_md=f"Seed/land/load the **{domain}** domain's raw datasets.",
    )
    def _dag():
        @task
        def seed():
            pipeline.seed_oltp(runtime.repo_root(), runtime.settings(), runtime.data_dir(), domain)

        @task
        def land():
            pipeline.land_files(runtime.repo_root(), runtime.settings(), runtime.data_dir(), domain)

        # Airflow lineage lives at the load boundary (ADR-0018): the loader consumes the source
        # (shop_oltp table / landing object) and produces the <domain>_raw tables, so the catalog
        # reads "source -> load_raw -> raw tables -> [dbt medallion] -> ports". raw_asset is for
        # scheduling the transform DAG.
        @task(outlets=[raw_asset(domain), *_raw_outlets(datasets)], inlets=_source_inlets(datasets))
        def load_raw():
            run_id = get_current_context()["run_id"]
            pipeline.load_raw(
                runtime.repo_root(), runtime.settings(), runtime.data_dir(), run_id, domain
            )

        upstream = []
        if has_oltp:
            upstream.append(seed())
        if has_landing:
            upstream.append(land())
        load = load_raw()
        for up in upstream:
            up >> load

    return _dag()
