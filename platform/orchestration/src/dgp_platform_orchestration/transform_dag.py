"""Airflow 3 DAG factory for the transform stage: dbt rendered per model by Cosmos, with
per-model write-audit-publish (ADR-0015).

``build_transform_dag(domain)`` returns one DAG per domain, scheduled on the domain's
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
from datetime import datetime

import pendulum
import yaml
from airflow.sdk import Param, TaskGroup, dag, task
from cosmos import DbtTaskGroup, ExecutionConfig, ProfileConfig, ProjectConfig, RenderConfig
from cosmos.constants import ExecutionMode, LoadMode, TestBehavior
from cosmos.operators.local import (
    DbtRunLocalOperator,
    DbtRunOperationLocalOperator,
    DbtTestLocalOperator,
)

from . import dbt_state, runtime, wap
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


def _date(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


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

    root = runtime.repo_root()
    pdir = wap.project_dir(root, domain)
    manifest = wap.load_manifest(root, domain)  # rendered from the manifest, never `dbt ls`
    names = wap.wap_products(root, domain)
    wap_models = wap.wap_models(manifest, domain, names)
    downstream = wap.plain_descendants(manifest, domain, names, quarantined=[])
    by_name = {n["name"]: uid for uid, n in manifest["nodes"].items()}

    # Cosmos runs each task from a temporary copy of the project, so the shared package and the
    # upstream manifests (dbt-loom) are addressed absolutely (see packages.yml/dbt_loom.config.yml).
    env = {
        "DGP_DBT_PACKAGE_DIR": str(root / "platform" / "transform" / "dgp_dbt"),
        "DGP_DBT_PROJECT_DIR": f"{pdir}/",
    }
    profile = ProfileConfig(
        profile_name=wap.project_name(domain),
        target_name="dev",
        profiles_yml_filepath=root / "platform" / "transform" / "profiles.yml",
    )
    project = ProjectConfig(
        dbt_project_path=pdir,
        manifest_path=pdir / "target" / "manifest.json",
        project_name=wap.project_name(domain),
        env_vars=env,
    )
    dbt_exe = shutil.which("dbt") or "dbt"
    execution = ExecutionConfig(execution_mode=ExecutionMode.LOCAL, dbt_executable_path=dbt_exe)
    # No Airflow dataset lineage from dbt tasks (ADR-0018): dbt owns raw -> ports lineage.
    op_args = {"install_deps": True, "append_env": True, "emit_datasets": False}

    def _render(**kw) -> RenderConfig:
        return RenderConfig(
            load_method=LoadMode.DBT_MANIFEST,
            test_behavior=TestBehavior.AFTER_EACH,
            emit_datasets=False,
            **kw,
        )

    def _op(cls, task_id: str, **kw):
        return cls(
            task_id=task_id,
            project_dir=str(pdir),
            profile_config=profile,
            dbt_executable_path=dbt_exe,
            env=env,
            **op_args,
            **kw,
        )

    @dag(
        dag_id=f"{domain}_transform",
        schedule=schedule,
        start_date=start_date or _START,
        catchup=catchup,
        default_args=args,
        max_active_runs=1,  # a backfill and a normal run must not merge into the same table
        render_template_as_native_obj=True,
        tags=tags or [domain, "transform"],
        params={
            "event_time_start": Param(
                None, type=["null", "string"], description="Backfill window start (YYYY-MM-DD)"
            ),
            "event_time_end": Param(
                None, type=["null", "string"], description="Backfill window end, exclusive"
            ),
            "full_refresh": Param(
                False, type="boolean", description="Rebuild incremental models in full"
            ),
        },  # fmt: skip
        doc_md=f"Build and write-audit-publish the **{domain}** output ports, one task per model "
        "(ADR-0015). Backfill: trigger with `event_time_start` / `event_time_end`.",
    )
    def _dag():
        core = DbtTaskGroup(
            group_id="models",
            project_config=project,
            profile_config=profile,
            execution_config=execution,
            render_config=_render(
                select=[f"package:{wap.project_name(domain)}"],
                exclude=[f"{n}+" for n in names],
            ),
            operator_args=op_args,
        )
        published = [core]  # must all succeed for save_state
        quarantines = []  # skipped on success; only grant waits on them
        publish_of: dict[str, object] = {}

        for m in wap_models:
            with TaskGroup(group_id=f"wap_{m.name}") as group:

                @task(task_id="plan")
                def plan(model: wap.WapModel = m, **context) -> dict:
                    p = context["params"]
                    window = wap.plan_window(
                        domain, model,
                        start=_date(p.get("event_time_start")),
                        end=_date(p.get("event_time_end")),
                        full_refresh=bool(p.get("full_refresh")),
                    )  # fmt: skip
                    span = window.args() if window else {}
                    return {
                        "prepare": {"model": model.name, "full_refresh": window is None},
                        "flags": window.flags() if window else [],
                        "window": {"model": model.name, **span},
                    }

                planned = plan()

                def plan_of(key: str, model: str = m.name) -> str:
                    return f"{{{{ ti.xcom_pull(task_ids='wap_{model}.plan')['{key}'] }}}}"

                wap_vars = {"wap": True, "wap_products": [m.name]}
                stage = _op(DbtRunOperationLocalOperator, "stage", macro_name="wap_prepare",
                            args=plan_of("prepare"))  # fmt: skip
                write = _op(DbtRunLocalOperator, "write", select=m.name, vars=wap_vars,
                            dbt_cmd_flags=plan_of("flags"))  # fmt: skip
                audit_view = _op(DbtRunOperationLocalOperator, "audit_view",
                                 macro_name="wap_audit_view", args=plan_of("window"))  # fmt: skip
                # A failed audit is a data verdict, not a flake: no retries.
                audit = _op(DbtTestLocalOperator, "audit", select=m.name, retries=0,
                            vars={**wap_vars, "wap_stage": "audit"})  # fmt: skip
                publish = _op(DbtRunOperationLocalOperator, "publish", macro_name="wap_promote",
                              args=plan_of("window"))  # fmt: skip
                quarantine = _op(DbtRunOperationLocalOperator, "quarantine",
                                 macro_name="wap_drop_candidate", args={"model": m.name},
                                 trigger_rule="one_failed")  # fmt: skip
                planned >> stage >> write >> audit_view >> audit >> publish
                [write, audit_view, audit] >> quarantine
            publish_of[m.name] = publish
            published.append(publish)
            quarantines.append(quarantine)

            # Per-model edges: start after this model's direct parents are published.
            for parent in manifest["parent_map"].get(by_name[m.name], []):
                pname = manifest["nodes"].get(parent, {}).get("name")
                if pname in publish_of:
                    publish_of[pname] >> group
                elif parent in core.tasks_map:
                    core.tasks_map[parent] >> group

        if downstream:
            after = DbtTaskGroup(
                group_id="downstream",
                project_config=project,
                profile_config=profile,
                execution_config=execution,
                render_config=_render(select=downstream),
                operator_args=op_args,
            )
            published.append(after)
            for name in downstream:
                for parent in manifest["parent_map"].get(by_name[name], []):
                    pname = manifest["nodes"].get(parent, {}).get("name")
                    if pname in publish_of:
                        publish_of[pname] >> after.tasks_map[by_name[name]]
                    elif parent in core.tasks_map:
                        core.tasks_map[parent] >> after.tasks_map[by_name[name]]

        # Masking views and consumer grants are dropped by dbt's relation swaps (ADR-0013), so
        # they are re-applied whatever happened above.
        @task(trigger_rule="all_done")
        def grant():
            access = runtime.repo_root() / "platform" / "access"
            for script in ("apply_masking.py", "grant_consumers.py"):
                subprocess.run([sys.executable, str(access / script)], check=True)

        # Only a run where every model published saves state and emits published/<domain>, which
        # triggers the consuming domains (a quarantine stops the cascade: proof P3).
        @task(outlets=[published_asset(domain)])
        def save_state():
            dbt_state.save(pdir / "target" / "manifest.json", domain)

        granted, saved = grant(), save_state()
        [*published, *quarantines] >> granted
        [*published, granted] >> saved

    return _dag()
