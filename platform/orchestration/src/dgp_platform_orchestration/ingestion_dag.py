"""Airflow 3 DAG factories for ingestion.

- ``build_acquire_dag()`` — one platform DAG that downloads the Olist dataset and
  publishes the OLIST_CACHE asset.
- ``build_ingestion_dag(domain)`` — one DAG per domain, scheduled on OLIST_CACHE,
  that seeds / lands / loads only that domain's datasets and publishes the domain's
  raw asset. A domain's DAG file is a single call to this factory, so domains write
  no orchestration code (ADR-0008); isolation between domains is proof P1.
"""

from __future__ import annotations

import pendulum
from airflow.sdk import dag, get_current_context, task
from dgp_platform_ingestion import pipeline
from dgp_platform_ingestion.config import load_datasets

from . import runtime
from .assets import OLIST_CACHE, raw_asset

_START = pendulum.datetime(2026, 1, 1, tz="UTC")
_DEFAULT_ARGS = {"retries": 2, "retry_delay": pendulum.duration(minutes=1)}


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

        @task(outlets=[raw_asset(domain)])
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
