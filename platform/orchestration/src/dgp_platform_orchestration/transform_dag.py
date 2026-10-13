"""Airflow 3 DAG factory for the transform stage (dbt + write-audit-publish).

``build_transform_dag(domain)`` returns one DAG per domain that builds the domain's
medallion models and publishes its output ports via write-audit-publish (ADR-0006).
It is scheduled on the domain's ``raw/<domain>`` asset and on the ``published/<dep>``
asset of every domain its aggregates consume — so cross-domain products rebuild only
after their inputs are published. It emits ``published/<domain>``.
"""

from __future__ import annotations

import subprocess
import sys

import pendulum
import yaml
from airflow.sdk import dag, task

from . import runtime
from .assets import published_asset, raw_asset

_START = pendulum.datetime(2026, 1, 1, tz="UTC")


def _upstream_domains(domain: str) -> list[str]:
    """Domains whose output ports this domain consumes (from data product inputs)."""
    deps: set[str] = set()
    dp_dir = runtime.repo_root() / "domains" / domain / "data_products"
    for p in dp_dir.glob("*.yaml"):
        defn = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        for ref in defn.get("inputs") or []:
            dom = str(ref).split(".")[0]
            if dom and dom != domain:
                deps.add(dom)
    return sorted(deps)


def build_transform_dag(domain: str):
    schedule = [raw_asset(domain)] + [published_asset(d) for d in _upstream_domains(domain)]

    @dag(
        dag_id=f"{domain}_transform",
        schedule=schedule,
        start_date=_START,
        catchup=False,
        default_args={"retries": 1, "retry_delay": pendulum.duration(minutes=1)},
        tags=[domain, "transform"],
        doc_md=f"Build and write-audit-publish the **{domain}** output ports.",
    )
    def _dag():
        @task(outlets=[published_asset(domain)])
        def publish():
            script = runtime.repo_root() / "platform" / "transform" / "publish.py"
            result = subprocess.run([sys.executable, str(script), "--domain", domain])
            if result.returncode != 0:
                # Write-audit-publish quarantined a bad build (proof P3); the previous
                # version is still published. Fail the task so the breach is visible.
                raise RuntimeError(f"{domain}: a build was quarantined by the audit (P3)")

        publish()

    return _dag()
