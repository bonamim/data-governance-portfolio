"""Airflow 3 DAG factory for the transform stage: dbt rendered per model by Cosmos, with
per-model write-audit-publish (ADR-0015).

``build_transform_dag(domain)`` returns one DAG per domain, scheduled on ANY of the domain's
``raw/<domain>`` asset and the ``published/<dep>`` asset of every domain it consumes, emitting
``published/<domain>`` when everything published. The graph, rendered from the dbt manifest
(``LoadMode.DBT_MANIFEST``: no ``dbt ls`` at parse time):

    models          every model except the opted-in ones and their descendants — one run + test
                    task group per model (Cosmos), so a failure names the model and retries it alone
    wap_<model>     per opted-in model: plan -> stage -> write -> audit_view -> audit -> publish,
                    with quarantine on failure (consumers keep the last good version)
    downstream      models that read a WAP model, after its publish
    grant           masking views + consumer grants, always (a rebuild drops them)
    save_state      the manifest to platform-state for Slim CI, only when everything published

Backfill: trigger the same DAG with params ``event_time_start`` / ``event_time_end`` (microbatch
WAP models audit and merge exactly that window) or ``full_refresh`` — never a cloned DAG.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pendulum
from airflow.sdk import DAG, Param, TaskGroup, dag, task
from cosmos import DbtTaskGroup, ExecutionConfig, ProfileConfig, ProjectConfig, RenderConfig
from cosmos.constants import ExecutionMode, LoadMode, TestBehavior
from cosmos.operators.local import (
    DbtRunLocalOperator,
    DbtRunOperationLocalOperator,
    DbtTestLocalOperator,
)
from dgp_governance import products

from . import dbt_state, runtime, wap
from .assets import published_asset, raw_asset

_START = pendulum.datetime(2026, 1, 1, tz="UTC")
# No Airflow dataset lineage from dbt tasks (ADR-0018): dbt owns the lineage from raw to the ports.
_OPERATOR_ARGS = {"install_deps": True, "append_env": True, "emit_datasets": False}
_PARAMS = {
    "event_time_start": Param(
        None, type=["null", "string"], description="Backfill window start (YYYY-MM-DD)"
    ),
    "event_time_end": Param(
        None, type=["null", "string"], description="Backfill window end, exclusive"
    ),
    "full_refresh": Param(False, type="boolean", description="Rebuild incremental models in full"),
}


def _upstream_domains(domain: str) -> list[str]:
    """Domains whose output ports this domain consumes (from its data products' inputs)."""
    inputs = (ref for product in products.data_products(domain) for ref in product.inputs)
    return sorted({ref.split(".")[0] for ref in inputs} - {"", domain})


def _date(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass
class _Dbt:
    """How the domain's dbt project runs under Cosmos: one place for its profile, project and
    environment, so every task group and operator is configured the same way."""

    domain: str
    root: Path
    pdir: Path = field(init=False)
    env: dict[str, str] = field(init=False)
    executable: str = field(init=False)
    profile: ProfileConfig = field(init=False)

    def __post_init__(self) -> None:
        self.pdir = wap.project_dir(self.root, self.domain)
        transform = self.root / "platform" / "core" / "transform"
        # Cosmos runs each task from a temporary copy of the project, so the shared package and
        # the upstream manifests (dbt-loom) are addressed absolutely (packages.yml,
        # dbt_loom.config.yml).
        self.env = {
            "DGP_DBT_PACKAGE_DIR": str(transform / "dgp_dbt"),
            "DGP_DBT_PROJECT_DIR": f"{self.pdir}/",
        }
        self.executable = shutil.which("dbt") or "dbt"
        self.profile = ProfileConfig(
            profile_name=wap.project_name(self.domain),
            target_name="dev",
            profiles_yml_filepath=transform / "profiles.yml",
        )

    def task_group(self, group_id: str, **render: object) -> DbtTaskGroup:
        """A Cosmos task group (one run + test task per model) for the selected models."""
        return DbtTaskGroup(
            group_id=group_id,
            project_config=ProjectConfig(
                dbt_project_path=self.pdir,
                manifest_path=self.pdir / "target" / "manifest.json",
                project_name=wap.project_name(self.domain),
                env_vars=self.env,
            ),
            profile_config=self.profile,
            execution_config=ExecutionConfig(
                execution_mode=ExecutionMode.LOCAL, dbt_executable_path=self.executable
            ),
            render_config=RenderConfig(
                load_method=LoadMode.DBT_MANIFEST,
                test_behavior=TestBehavior.AFTER_EACH,
                emit_datasets=False,
                **render,
            ),
            operator_args=_OPERATOR_ARGS,
        )

    def operator(self, cls: type, task_id: str, **kw: object):
        """One dbt command as a single task."""
        return cls(
            task_id=task_id,
            project_dir=str(self.pdir),
            profile_config=self.profile,
            dbt_executable_path=self.executable,
            env=self.env,
            **_OPERATOR_ARGS,
            **kw,
        )


def _wap_group(dbt: _Dbt, model: wap.WapModel):
    """One opted-in model's write-audit-publish: plan -> stage -> write -> audit_view -> audit ->
    publish, with quarantine when any step fails. Returns (group, publish, quarantine)."""
    domain = dbt.domain
    with TaskGroup(group_id=f"wap_{model.name}") as group:

        @task(task_id="plan")
        def plan(model: wap.WapModel = model, **context) -> dict:
            p = context["params"]
            window = wap.plan_window(
                domain,
                model,
                start=_date(p.get("event_time_start")),
                end=_date(p.get("event_time_end")),
                full_refresh=bool(p.get("full_refresh")),
            )
            span = window.args() if window else {}
            return {
                "prepare": {"model": model.name, "full_refresh": window is None},
                "flags": window.flags() if window else [],
                "window": {"model": model.name, **span},
            }

        def planned(key: str) -> str:
            return f"{{{{ ti.xcom_pull(task_ids='wap_{model.name}.plan')['{key}'] }}}}"

        wap_vars = {"wap": True, "wap_products": [model.name]}
        run_op = DbtRunOperationLocalOperator
        stage = dbt.operator(run_op, "stage", macro_name="wap_prepare", args=planned("prepare"))
        write = dbt.operator(
            DbtRunLocalOperator,
            "write",
            select=model.name,
            vars=wap_vars,
            dbt_cmd_flags=planned("flags"),
        )
        audit_view = dbt.operator(
            run_op, "audit_view", macro_name="wap_audit_view", args=planned("window")
        )
        # A failed audit is a data verdict, not a flake: no retries.
        audit = dbt.operator(
            DbtTestLocalOperator,
            "audit",
            select=model.name,
            retries=0,
            vars={**wap_vars, "wap_stage": "audit"},
        )
        publish = dbt.operator(run_op, "publish", macro_name="wap_promote", args=planned("window"))
        quarantine = dbt.operator(
            run_op,
            "quarantine",
            macro_name="wap_drop_candidate",
            args={"model": model.name},
            trigger_rule="one_failed",
        )
        plan() >> stage >> write >> audit_view >> audit >> publish
        [write, audit_view, audit] >> quarantine
    return group, publish, quarantine


def _after_parents(
    manifest: dict, node: str, target: object, publish_of: dict, core: DbtTaskGroup
) -> None:
    """Start ``target`` after the model ``node``'s direct parents: a WAP model's publish, or the
    parent's task in the main group."""
    for parent in manifest["parent_map"].get(node, []):
        name = manifest["nodes"].get(parent, {}).get("name")
        if name in publish_of:
            publish_of[name] >> target
        elif parent in core.tasks_map:
            core.tasks_map[parent] >> target


def build_transform_dag(
    domain: str,
    *,
    start_date: pendulum.DateTime | None = None,
    catchup: bool = False,
    retries: int = 1,
    retry_delay: pendulum.Duration | None = None,
    tags: list[str] | None = None,
    default_args: dict | None = None,
) -> DAG:
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
    # ANY input changing rebuilds the domain (a list would wait for ALL of them): new raw data, or a
    # new version of an upstream domain's port it consumes (proof P4). max_active_runs=1 folds a
    # burst of upstream events into one rebuild.
    schedule = raw_asset(domain)
    for upstream in _upstream_domains(domain):
        schedule = schedule | published_asset(upstream)

    root = runtime.repo_root()
    dbt = _Dbt(domain, root)
    manifest = wap.load_manifest(root, domain)  # rendered from the manifest, never `dbt ls`
    names = wap.wap_products(root, domain)
    wap_models = wap.wap_models(manifest, domain, names)
    downstream = wap.plain_descendants(manifest, domain, names, quarantined=[])
    by_name = {n["name"]: uid for uid, n in manifest["nodes"].items()}

    @dag(
        dag_id=f"{domain}_transform",
        schedule=schedule,
        start_date=start_date or _START,
        catchup=catchup,
        default_args=args,
        max_active_runs=1,  # a backfill and a normal run must not merge into the same table
        render_template_as_native_obj=True,
        tags=tags or [domain, "transform"],
        params=_PARAMS,
        doc_md=f"Build and write-audit-publish the **{domain}** output ports, one task per model "
        "(ADR-0015). Backfill: trigger with `event_time_start` / `event_time_end`.",
    )
    def _dag():
        core = dbt.task_group(
            "models",
            select=[f"package:{wap.project_name(domain)}"],
            exclude=[f"{n}+" for n in names],
        )
        published = [core]  # must all succeed for save_state
        quarantines = []  # skipped on success; only grant waits on them
        publish_of: dict[str, object] = {}

        for m in wap_models:
            group, publish, quarantine = _wap_group(dbt, m)
            publish_of[m.name] = publish
            published.append(publish)
            quarantines.append(quarantine)
            # Per-model edges: start after this model's direct parents are published.
            _after_parents(manifest, by_name[m.name], group, publish_of, core)

        if downstream:
            after = dbt.task_group("downstream", select=downstream)
            published.append(after)
            for name in downstream:
                node = by_name[name]
                _after_parents(manifest, node, after.tasks_map[node], publish_of, core)

        # Masking views and consumer grants are dropped by dbt's relation swaps (ADR-0013), so
        # they are re-applied whatever happened above.
        @task(trigger_rule="all_done")
        def grant():
            for tool in ("apply_masking", "apply_grants"):
                subprocess.run([sys.executable, "-m", f"dgp_platform_access.{tool}"], check=True)

        # Only a run where every model published saves state and emits published/<domain>, which
        # triggers the consuming domains (a quarantine stops the cascade: proof P3).
        @task(outlets=[published_asset(domain)])
        def save_state():
            dbt_state.save(dbt.pdir / "target" / "manifest.json", domain)

        granted, saved = grant(), save_state()
        [*published, *quarantines] >> granted
        [*published, granted] >> saved

    return _dag()
