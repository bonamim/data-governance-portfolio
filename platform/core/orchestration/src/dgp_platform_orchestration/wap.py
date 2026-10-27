"""Per-model write-audit-publish for the transform layer (ADR-0014 policy, ADR-0015 mechanism).

Shared by ``platform/core/transform/publish.py`` and the Airflow transform DAG, so the two paths run
the same steps. No Airflow import here: it sequences the dbt CLI and the ``dgp_dbt`` macros.

A domain is published in three parts:

  core          every model except the opted-in ones and their descendants, built in place
  wap models    one at a time: stage -> build to ``<name>__candidate`` + audit -> promote or
                quarantine. One bad model does not block the others (per-model, not domain-atomic)
  descendants   models downstream of a promoted WAP model, built after it (skipped if quarantined)

Opt-in is per data product (``publish: {audit: required}``). A microbatch model is audited per
batch window and merged. Any other table/incremental model is rebuilt in full into the candidate.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import yaml
from dgp_platform_ingestion.settings import PostgresSettings

from . import dbt_results

log = logging.getLogger("dgp.wap")

# Dependency order: a domain is published after the domains whose ports it consumes.
DOMAINS = ["customer", "customer_experience", "sales"]
TS = "%Y-%m-%dT%H:%M:%S"


@dataclass(frozen=True)
class WapModel:
    name: str
    schema: str
    alias: str
    materialized: str
    incremental_strategy: str | None
    event_time: str | None
    batch_size: str | None
    lookback: int

    @property
    def microbatch(self) -> bool:
        return self.materialized == "incremental" and self.incremental_strategy == "microbatch"


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime

    def flags(self) -> list[str]:
        return [
            "--event-time-start",
            self.start.strftime(TS),
            "--event-time-end",
            self.end.strftime(TS),
        ]

    def args(self) -> dict:
        return {"window_start": self.start.strftime(TS), "window_end": self.end.strftime(TS)}


# ------------------------------------------------------------------------------- the plan
def project_dir(repo_root: Path, domain: str) -> Path:
    return repo_root / "domains" / domain / "dbt"


def project_name(domain: str) -> str:
    return f"dgp_{domain}"


def wap_products(repo_root: Path, domain: str) -> list[str]:
    """The domain's products that opt in to write-audit-publish (``publish.audit: required``)."""
    out = []
    for p in sorted((repo_root / "domains" / domain / "data_products").glob("*.yaml")):
        defn = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if (defn.get("publish") or {}).get("audit") == "required":
            out.append(defn["name"])
    return out


def load_manifest(repo_root: Path, domain: str) -> dict:
    path = project_dir(repo_root, domain) / "target" / "manifest.json"
    if not path.exists():
        raise FileNotFoundError(f"{path} missing. Run `make dbt-manifests` first")
    return json.loads(path.read_text(encoding="utf-8"))


def _models(manifest: dict, domain: str) -> dict[str, dict]:
    """unique_id -> node for this project's own models (not dbt-loom's injected ones)."""
    return {
        uid: n
        for uid, n in manifest["nodes"].items()
        if n["resource_type"] == "model" and n["package_name"] == project_name(domain)
    }


def wap_models(manifest: dict, domain: str, names: list[str]) -> list[WapModel]:
    """The opted-in models, upstream first (a WAP model may consume another)."""
    out = []
    models = _models(manifest, domain)
    for n in models.values():
        if n["name"] in names:
            cfg = n["config"]
            out.append(
                WapModel(
                    name=n["name"],
                    schema=n["schema"],
                    alias=n["alias"],
                    materialized=cfg.get("materialized"),
                    incremental_strategy=cfg.get("incremental_strategy"),
                    event_time=cfg.get("event_time"),
                    batch_size=cfg.get("batch_size"),
                    lookback=int(cfg.get("lookback") or 1),
                )
            )
    # An upstream model has strictly fewer opted-in ancestors than its descendants.
    return sorted(
        out, key=lambda m: (len(set(ancestors(manifest, domain, m.name)) & set(names)), m.name)
    )


def _walk(manifest: dict, domain: str, names: list[str], edges: str) -> list[str]:
    models = _models(manifest, domain)
    by_name = {n["name"]: uid for uid, n in models.items()}
    seen: set[str] = set()
    todo = [by_name[n] for n in names if n in by_name]
    while todo:
        for nxt in manifest[edges].get(todo.pop(), []):
            if nxt in models and nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    return sorted(models[u]["name"] for u in seen if models[u]["name"] not in names)


def descendants(manifest: dict, domain: str, names: list[str]) -> list[str]:
    """This project's models downstream of ``names`` (excluding them)."""
    return _walk(manifest, domain, names, "child_map")


def ancestors(manifest: dict, domain: str, name: str) -> list[str]:
    """This project's models upstream of ``name``."""
    return _walk(manifest, domain, [name], "parent_map")


def plain_descendants(
    manifest: dict, domain: str, wap_names: list[str], quarantined: list[str]
) -> list[str]:
    """Non-WAP models downstream of the WAP models, built after their promote. Those downstream
    of a quarantined model are left alone: they keep reading its last good version."""
    skip = set(descendants(manifest, domain, quarantined)) | set(wap_names)
    return [d for d in descendants(manifest, domain, wap_names) if d not in skip]


# ---------------------------------------------------------------------------- windows
def _floor(ts: datetime, size: str) -> datetime:
    ts = ts.replace(minute=0, second=0, microsecond=0, tzinfo=None)
    if size == "hour":
        return ts
    ts = ts.replace(hour=0)
    if size == "day":
        return ts
    ts = ts.replace(day=1)
    return ts if size == "month" else ts.replace(month=1)


def _step(ts: datetime, size: str, n: int) -> datetime:
    """``ts`` (a batch boundary) moved by ``n`` batches."""
    if size == "hour":
        return ts + timedelta(hours=n)
    if size == "day":
        return ts + timedelta(days=n)
    if size == "month":
        months = ts.year * 12 + ts.month - 1 + n
        return ts.replace(year=months // 12, month=months % 12 + 1)
    return ts.replace(year=ts.year + n)


def batch_window(
    model: WapModel,
    now: datetime,
    start: datetime | None = None,
    end: datetime | None = None,
) -> Window:
    """The batch window to build and merge, aligned to whole batches.

    A normal run takes the batch ``now`` falls in plus ``lookback`` earlier ones (dbt's own
    default). A backfill passes ``start``/``end``. Both build and merge use the same window, so
    a merge replaces exactly what the candidate was built for.
    """
    size = model.batch_size or "day"
    if start is None:
        start = _step(_floor(now, size), size, -model.lookback)
    lo = _floor(start, size)
    hi = _floor(end or now, size)
    if end is None or hi < end.replace(tzinfo=None):
        hi = _step(hi, size, 1)  # include the partial batch the end falls in
    if hi <= lo:
        raise ValueError(f"empty backfill window {lo:%Y-%m-%d} .. {hi:%Y-%m-%d}")
    return Window(lo, hi)


# ------------------------------------------------------------------------- warehouse I/O
def _conninfo(domain: str) -> str:
    """The domain role's connection: a domain's batches are written as the domain itself."""
    password = os.environ[f"{domain.upper()}_ROLE_PASSWORD"]
    return PostgresSettings.analytics(f"{domain}_role", password).conninfo()


def port_is_table(domain: str, model: WapModel) -> bool:
    """Is the published relation an existing table (so batches can be merged into it)?"""
    with psycopg.connect(_conninfo(domain)) as conn:
        row = conn.execute(
            "SELECT relkind FROM pg_class WHERE oid = to_regclass(%s)",
            (f"{model.schema}.{model.alias}",),
        ).fetchone()
    return bool(row) and row[0] in ("r", "p")


def dbt(repo_root: Path, domain: str, *args: str) -> int:
    """Run dbt in the domain's project (cwd, so dbt-loom finds its config)."""
    exe = shutil.which("dbt") or "dbt"
    profiles = repo_root / "platform" / "core" / "transform"
    cmd = [exe, *args, "--profiles-dir", str(profiles)]
    log.info("dbt %s (%s)", " ".join(args), domain)
    return subprocess.run(cmd, cwd=project_dir(repo_root, domain)).returncode


def _op(repo_root: Path, domain: str, macro: str, args: dict) -> int:
    return dbt(repo_root, domain, "run-operation", macro, "--args", json.dumps(args))


# ------------------------------------------------------------------------------- steps
def _wap_vars(model: WapModel, stage: str = "") -> list[str]:
    v = {"wap": True, "wap_products": [model.name], **({"wap_stage": stage} if stage else {})}
    # target/wap keeps the candidate-aliased manifest out of target/ (read by dbt-loom and Cosmos).
    return ["--vars", json.dumps(v), "--target-path", "target/wap"]


def dbt_audit(repo_root: Path, domain: str, model: WapModel, window: Window | None) -> int:
    """WRITE + AUDIT one model into its candidate. 0 if the audit passed.

    The tests run against ``<name>__audit``: the table consumers would get after the promote. For
    a microbatch window the candidate holds only the batches, so the view is the published table
    with that window replaced, and table-level rules (row counts, uniqueness) judge the whole table.
    """
    flags = window.flags() if window else []
    run = dbt(repo_root, domain, "run", "--select", model.name, *_wap_vars(model), *flags)
    if run:
        return run
    view_args = {"model": model.name, **(window.args() if window else {})}
    if _op(repo_root, domain, "wap_audit_view", view_args):
        return 1
    audit = dbt(repo_root, domain, "test", "--select", model.name, *_wap_vars(model, "audit"))
    dbt_results.publish(project_dir(repo_root, domain) / "target" / "wap", stage="audit")
    return audit


def plan_window(
    domain: str,
    model: WapModel,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    full_refresh: bool = False,
    now: datetime | None = None,
) -> Window | None:
    """The batch window to build and merge, or None to rebuild the model in full: it is not a
    microbatch model, a full refresh was asked for, or there is no published table yet."""
    if model.microbatch and not full_refresh and port_is_table(domain, model):
        return batch_window(model, now or datetime.now(UTC), start, end)
    if start or end:
        why = "no published table to merge into yet" if model.microbatch else "not microbatch"
        log.info("%s: %s, so the backfill window does not apply, rebuilt in full", model.name, why)
    return None


def audit_and_promote(
    repo_root: Path,
    domain: str,
    model: WapModel,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    full_refresh: bool = False,
    now: datetime | None = None,
) -> bool:
    """Write-audit-publish one model. True if it was published, False if quarantined."""
    window = plan_window(domain, model, start=start, end=end, full_refresh=full_refresh, now=now)
    if _op(repo_root, domain, "wap_prepare", {"model": model.name, "full_refresh": window is None}):
        raise RuntimeError(f"{domain}.{model.name}: wap_prepare failed")
    if dbt_audit(repo_root, domain, model, window) == 0:
        args = {"model": model.name, **(window.args() if window else {})}
        if _op(repo_root, domain, "wap_promote", args):
            raise RuntimeError(f"{domain}.{model.name}: promote failed (candidate kept)")
        return True
    _op(repo_root, domain, "wap_drop_candidate", {"model": model.name})
    log.warning(
        "QUARANTINED %s.%s: audit failed, consumers keep the last good version", domain, model.name
    )
    return False


def publish_domain(
    repo_root: Path,
    domain: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    full_refresh: bool = False,
) -> bool:
    """Build and publish a whole domain (the CLI path. Airflow runs the same parts as tasks)."""
    if dbt(repo_root, domain, "deps") or dbt(repo_root, domain, "parse"):
        raise RuntimeError(f"{domain}: dbt deps/parse failed")
    manifest = load_manifest(repo_root, domain)
    models = wap_models(manifest, domain, wap_products(repo_root, domain))
    names = [m.name for m in models]
    refresh = ["--full-refresh"] if full_refresh else []

    ok = True
    core = ["build", *refresh]
    if names:
        core += ["--exclude", *[f"{n}+" for n in names]]
    if dbt(repo_root, domain, *core):
        ok = False
    target = project_dir(repo_root, domain) / "target"
    dbt_results.publish(target)
    published, quarantined = [], []
    for m in models:
        if set(ancestors(manifest, domain, m.name)) & set(quarantined):
            log.warning("skipped %s.%s: an upstream audit failed", domain, m.name)
            quarantined.append(m.name)
        elif audit_and_promote(
            repo_root, domain, m, start=start, end=end, full_refresh=full_refresh
        ):
            published.append(m.name)
        else:
            quarantined.append(m.name)
    ok = ok and not quarantined
    downstream = plain_descendants(manifest, domain, names, quarantined)
    if downstream:
        if dbt(repo_root, domain, "build", *refresh, "--select", *downstream):
            ok = False
        dbt_results.publish(target)
    return ok
