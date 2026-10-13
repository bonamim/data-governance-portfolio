"""Airflow 3 DAG factory for the transform stage (dbt + write-audit-publish).

``build_transform_dag(domain)`` returns one DAG per domain that builds the domain's
medallion models and publishes its output ports via write-audit-publish (ADR-0006).
It is scheduled on the domain's ``raw/<domain>`` asset and on the ``published/<dep>``
asset of every domain its aggregates consume — so cross-domain products rebuild only
after their inputs are published. It emits ``published/<domain>``.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pendulum
import yaml
from airflow.sdk import dag, task

from . import runtime
from .assets import published_asset, raw_asset

_START = pendulum.datetime(2026, 1, 1, tz="UTC")
_DB = os.environ.get("ANALYTICS_DB", "analytics")


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


def _port_location(owner: str, product: str) -> tuple[str, str] | None:
    """Physical (schema, table) of an output port, from its dbt model's layer folder."""
    models = runtime.repo_root() / "domains" / owner / "dbt" / "models"
    for path in models.rglob(f"{product}.sql"):
        return f"{owner}_{path.parent.name}", product
    return None


def _dataset(schema: str, table: str):
    """A DataHub lineage entity matching the Postgres/dbt ingestion URN exactly (so the edge
    lands on the real table). Lazy-imported so the factory still works without the plugin."""
    try:
        from datahub_airflow_plugin.entities import Dataset
    except ImportError:
        return None
    return Dataset(platform="postgres", name=f"{_DB}.{schema}.{table}", env="PROD")


def _output_port_datasets(domain: str) -> list:
    """This domain's output ports — the publish task's outputs ("which DAG builds this port")."""
    out = []
    for p in (runtime.repo_root() / "domains" / domain / "data_products").glob("*.yaml"):
        defn = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        loc = _port_location(domain, defn.get("name", ""))
        if loc and (ds := _dataset(*loc)):
            out.append(ds)
    return out


def _input_port_datasets(domain: str) -> list:
    """Upstream output ports this domain consumes — the publish task's inputs (cross-domain)."""
    out = []
    for p in (runtime.repo_root() / "domains" / domain / "data_products").glob("*.yaml"):
        defn = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        for ref in defn.get("inputs") or []:
            parts = str(ref).split(".")
            if len(parts) == 2 and parts[0] != domain and (loc := _port_location(*parts)):
                if ds := _dataset(*loc):
                    out.append(ds)
    return out


def build_transform_dag(
    domain: str,
    *,
    start_date: pendulum.DateTime | None = None,
    catchup: bool = False,
    retries: int = 1,
    retry_delay: pendulum.Duration | None = None,
    tags: list[str] | None = None,
    default_args: dict | None = None,
):
    """Build one transform DAG for ``domain``.

    The platform ships paved defaults; a domain overrides only what it needs (self-serve
    platform, federated governance — ADR-0003/0008/0015). Keys in ``default_args`` win over
    the ``retries`` / ``retry_delay`` shorthands.
    """
    args = {
        "retries": retries,
        "retry_delay": retry_delay or pendulum.duration(minutes=1),
        **(default_args or {}),
    }
    schedule = [raw_asset(domain)] + [published_asset(d) for d in _upstream_domains(domain)]
    # DataHub lineage (ADR-0018): the publish task PRODUCES the domain's output-port tables (the
    # "which DAG builds/publishes this dbt model" link) and CONSUMES the upstream ports it reads.
    # dbt owns the internal raw -> bronze -> silver -> gold lineage; Airflow sits at the load
    # boundary (ingestion) and the publish boundary (here). published/<domain> stays for scheduling.
    outlets = [published_asset(domain), *_output_port_datasets(domain)]
    inlets = _input_port_datasets(domain)

    @dag(
        dag_id=f"{domain}_transform",
        schedule=schedule,
        start_date=start_date or _START,
        catchup=catchup,
        default_args=args,
        tags=tags or [domain, "transform"],
        doc_md=f"Build and write-audit-publish the **{domain}** output ports.",
    )
    def _dag():
        @task(outlets=outlets, inlets=inlets)
        def publish():
            script = runtime.repo_root() / "platform" / "transform" / "publish.py"
            result = subprocess.run([sys.executable, str(script), "--domain", domain])
            if result.returncode != 0:
                # Write-audit-publish quarantined a bad build (proof P3); the previous
                # version is still published. Fail the task so the breach is visible.
                raise RuntimeError(f"{domain}: a build was quarantined by the audit (P3)")

        publish()

    return _dag()
