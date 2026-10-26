"""Airflow 3 DAG factories for ingestion (ADR-0016: event-driven).

- ``build_acquire_dag()`` — downloads the Olist dataset and publishes OLIST_CACHE.
- ``build_simulator_dag()`` — **PoC only**: stands in for the source systems. ``seed`` fills the
  operational database (publishes SHOP_OLTP) and ``deliver`` acts as each partner, putting its
  files into the landing inbox with write-only credentials. In production both are real producers
  and this DAG is not deployed.
- ``build_ingestion_dag(domain)`` — one DAG per domain, a single factory call per domain
  (ADR-0008; isolation is proof P1). It is fired by **arrivals**: the object store notifies the
  arrival queue, an event trigger fires ``landing/<domain>`` (windowed: one run per burst), and
  OLTP datasets follow SHOP_OLTP. One task per step, so the order is visible (ADR-0025)::

      contract_gate -> load_raw -> raw/<domain>
                    -> publish_quality

- ``build_landing_sla_dag()`` — every 15 minutes, checks each landing dataset's due partition
  against its contract deadline and alerts the owning domain on a missing, incomplete or rejected
  delivery.

``contract_gate`` files every inbox delivery to accepted/ or quarantine/ and loads nothing;
``load_raw`` loads only accepted files no load has picked up yet, so it can be retried without
re-checking. Both carry lineage into the catalog.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pendulum
from airflow.sdk import DAG, dag, get_current_context, task
from dgp_platform_ingestion import layout, pipeline, sla
from dgp_platform_ingestion.config import load_datasets

from . import runtime
from .assets import OLIST_CACHE, SHOP_OLTP, landing_asset, raw_asset

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


def _landing_entity(d):
    """The landing dataset: the S3 entity ``make catalog-quality`` publishes with the gate's
    assertions (ADR-0024)."""
    return _dataset("s3", layout.catalog_name(_ARCHIVE, d.source_system, d.name))


def _landing_outlets(datasets) -> list:
    """What the gate produces: the accepted landing datasets."""
    return [ds for d in datasets if d.source_type == "landing" and (ds := _landing_entity(d))]


def _source_inlets(datasets) -> list:
    """The load's upstream sources: the shop_oltp table (OLTP) or the landing dataset (files)."""
    out = []
    for d in datasets:
        if d.source_type == "oltp":
            ds = _dataset("postgres", f"shop_oltp.shop.{d.oltp_table}")
        else:
            ds = _landing_entity(d)
        if ds:
            out.append(ds)
    return out


def build_acquire_dag() -> DAG:
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


def build_simulator_dag() -> DAG:
    @dag(
        dag_id="platform_source_simulator",
        schedule=[OLIST_CACHE],
        start_date=_START,
        catchup=False,
        default_args=_DEFAULT_ARGS,
        tags=["platform", "simulator", "poc-only"],
        doc_md="**PoC only** — stands in for the source systems: seeds the operational database "
        "and delivers each partner's files into the landing inbox. Not deployed in production, "
        "where real producers do both (ADR-0016).",
    )
    def _dag():
        @task(outlets=[SHOP_OLTP])
        def seed():
            pipeline.seed_oltp(runtime.repo_root(), runtime.settings(), runtime.data_dir())

        @task
        def deliver():
            # A daily file carries the previous business day, as the contracts' deadline expects.
            yesterday = pendulum.now("UTC").subtract(days=1).to_date_string()
            pipeline.land_files(
                runtime.repo_root(),
                runtime.settings(),
                runtime.data_dir(),
                partition_date=yesterday,
            )

        seed()
        deliver()

    return _dag()


def build_landing_sla_dag() -> DAG:
    @dag(
        dag_id="platform_landing_sla",
        schedule="*/15 * * * *",
        start_date=_START,
        catchup=False,
        max_active_runs=1,
        default_args=_DEFAULT_ARGS,
        tags=["platform", "ingestion", "sla"],
        doc_md="Checks every landing dataset's due partition against its contract deadline "
        "(`timeOfAvailability`) and alerts the owning domain once per missed partition.",
    )
    def _dag():
        @task
        def check_deadlines() -> dict:
            findings = sla.check(runtime.repo_root(), runtime.settings())
            return {f"{f.dataset} {f.partition}": f.state for f in findings}

        check_deadlines()

    return _dag()


def build_ingestion_dag(domain: str) -> DAG:
    # Read at parse time (repo is mounted) to decide which steps this domain needs.
    datasets = load_datasets(runtime.repo_root(), domain)
    has_oltp = any(d.source_type == "oltp" for d in datasets)
    landing = [d for d in datasets if d.source_type == "landing"]
    triggers = [SHOP_OLTP] if has_oltp else []
    if landing:
        prefixes = [layout.inbox_prefix(d.source_system, d.name) for d in landing]
        triggers.append(landing_asset(domain, prefixes))
    schedule = triggers[0]
    for t in triggers[1:]:
        schedule = schedule | t  # either source fires the run

    @dag(
        dag_id=f"{domain}_ingestion",
        schedule=schedule,
        start_date=_START,
        catchup=False,
        # One run at a time per domain: two overlapping runs would list the same inbox file and
        # gate it twice (the ledger hand-off assumes a single gate/load per dataset at a time).
        # Arrivals during a run queue one more run, which drains whatever came in meanwhile.
        max_active_runs=1,
        default_args=_DEFAULT_ARGS,
        tags=[domain, "ingestion"],
        doc_md=f"Gate and load the **{domain}** domain's raw datasets, fired by arrivals "
        "(ADR-0016).",
    )
    def _dag():
        # The quality check, before anything reaches the warehouse (ADR-0024/0025): every inbox
        # delivery is filed to accepted/ (landing dataset) or quarantine/. It loads nothing.
        @task(outlets=_landing_outlets(datasets))
        def contract_gate():
            run_id = get_current_context()["run_id"]
            pipeline.contract_gate(runtime.repo_root(), runtime.settings(), run_id, domain)

        # Airflow lineage at the load boundary (ADR-0018): the loader consumes the source
        # (shop_oltp table / accepted landing dataset) and produces the <domain>_raw tables, so the
        # catalog reads "source -> load_raw -> raw tables -> [dbt medallion] -> ports". raw_asset
        # is for scheduling the transform DAG.
        @task(outlets=[raw_asset(domain), *_raw_outlets(datasets)], inlets=_source_inlets(datasets))
        def load_raw():
            run_id = get_current_context()["run_id"]
            pipeline.load_raw(runtime.repo_root(), runtime.settings(), run_id, domain)

        # The gate's per-rule results and each file's arrival reach DataHub right after every gate
        # run (ADR-0016/0024). It hangs off the gate, not the load, so a failed load cannot hide a
        # quarantine.
        @task
        def publish_quality():
            module = "dgp_platform_catalog.publish_landing_quality"
            subprocess.run([sys.executable, "-m", module, "--domain", domain], check=True)

        load = load_raw()
        if landing:
            gate = contract_gate()
            gate >> load
            gate >> publish_quality()

    return _dag()
