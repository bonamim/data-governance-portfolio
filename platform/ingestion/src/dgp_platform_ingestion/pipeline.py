"""Ingestion orchestration: acquire → seed → land → load-raw.

These functions are the self-serve platform capability. They are driven entirely
by the domains' ``sources.yml`` and know nothing dataset-specific, so a new domain
is onboarded with config alone (ADR-0008).

Flow for the M1 production slice:
  acquire   Download the Olist dataset from Kaggle into a local cache.
  seed      Load operational CSVs into shop_oltp (stands in for the source system).
  land      PoC partner simulator: PUT file-sourced CSVs into the landing inbox as the
            partner (write-only credentials, the partner's own file names).
  load-raw  Load every dataset into <domain>_raw in analytics, recording the ingestion
            ledger. OLTP sources are read from shop_oltp; landing deliveries are drained
            from the inbox through the contract gate into accepted/ or quarantine/
            (ADR-0024).
  replay    Rebuild raw from the sources of record: shop_oltp + the archive's accepted/
            files for a partition (restore drill, backfill). Never touches the inbox.
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from dgp_platform_quality.contract import Contract, load_contract_for
from dgp_platform_quality.engine import Assertion, ValidationResult, validate_file

from . import db, layout, ledger
from .config import Dataset, load_datasets, olist_files
from .layout import FilenameMismatch, LandingRules
from .objectstore import ObjectRef, ObjectStore
from .settings import Settings

log = logging.getLogger("dgp.ingestion")


def _today_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _oltp_datasets(datasets: list[Dataset]) -> list[Dataset]:
    return [d for d in datasets if d.source_type == "oltp"]


def _landing_datasets(datasets: list[Dataset]) -> list[Dataset]:
    return [d for d in datasets if d.source_type == "landing"]


# --------------------------------------------------------------------- acquire
def acquire(repo_root: Path, settings: Settings, data_dir: Path) -> Path:
    """Download and unzip the Olist dataset; return the cache directory."""
    cache = data_dir / "olist"
    cache.mkdir(parents=True, exist_ok=True)
    needed = olist_files(load_datasets(repo_root))

    missing = [f for f in needed if not (cache / f).exists()]
    if not missing:
        log.info("all %d Olist files already cached in %s", len(needed), cache)
        return cache

    # Imported lazily: the kaggle client authenticates on import and would fail
    # for commands that don't need it (e.g. load-raw on already-cached data).
    from kaggle.api.kaggle_api_extended import KaggleApi

    api = KaggleApi()
    api.authenticate()
    log.info("downloading %s (%d files needed)", settings.kaggle_dataset, len(missing))
    api.dataset_download_files(settings.kaggle_dataset, path=str(cache), unzip=True, quiet=False)

    still_missing = [f for f in needed if not (cache / f).exists()]
    if still_missing:
        raise RuntimeError(f"Olist files not found after download: {still_missing}")
    return cache


# ------------------------------------------------------------------------ seed
def seed_oltp(
    repo_root: Path, settings: Settings, data_dir: Path, domain: str | None = None
) -> None:
    """Load operational CSVs into shop_oltp (the stand-in source system)."""
    cache = data_dir / "olist"
    datasets = _oltp_datasets(load_datasets(repo_root, domain))
    with db.connect(settings.oltp_loader) as conn:
        for d in datasets:
            csv_path = cache / d.olist_file
            if not csv_path.exists():
                raise FileNotFoundError(f"{csv_path} missing — run acquire first")
            rows = db.load_csv_file(conn, "shop", d.oltp_table, csv_path)
            log.info("seeded shop.%s (%d rows)", d.oltp_table, rows)
        conn.commit()


# ------------------------------------------------------------------------ land
def land_files(
    repo_root: Path,
    settings: Settings,
    data_dir: Path,
    domain: str | None = None,
    partition_date: str | None = None,
) -> None:
    """PoC partner simulator: deliver file-sourced CSVs into the landing inbox.

    Acts *as the partner*: authenticates with the source's write-only credentials (it can put,
    not read, list or delete) and uses the partner's own file name for the business date. In
    production partners push on their own; this function is not deployed (ADR-0016).
    """
    cache = data_dir / "olist"
    business_date = datetime.strptime(partition_date or _today_utc(), "%Y-%m-%d")
    for d in _landing_datasets(load_datasets(repo_root, domain)):
        csv_path = cache / d.olist_file
        if not csv_path.exists():
            raise FileNotFoundError(f"{csv_path} missing — run acquire first")
        access, secret = settings.objectstore.partner_credentials(d.source_system)
        partner = ObjectStore(settings.objectstore, access, secret)
        key = layout.inbox_prefix(d.source_system, d.name) + d.delivered_name(business_date)
        partner.upload_file(csv_path, partner.inbox, key)
        log.info("partner %s delivered s3://%s/%s", d.source_system, partner.inbox, key)


# -------------------------------------------------------------------- load-raw
def _load_oltp(analytics: psycopg.Connection, settings: Settings, datasets, run_id: str) -> None:
    """OLTP-sourced: stream shop_oltp -> temp CSV -> <domain>_raw."""
    oltp_sets = _oltp_datasets(datasets)
    if not oltp_sets:
        return
    with db.connect(settings.oltp_reader) as oltp, tempfile.TemporaryDirectory() as tmp:
        for d in oltp_sets:
            dump = Path(tmp) / f"{d.oltp_table}.csv"
            db.export_table_to_csv(oltp, "shop", d.oltp_table, dump)
            rows = db.load_csv_file(analytics, d.raw_schema, d.raw_table, dump)
            ledger.record(
                analytics,
                source_type="table",
                source_identifier=f"shop_oltp.shop.{d.oltp_table}",
                target_schema=d.raw_schema,
                target_table=d.raw_table,
                row_count=rows,
                status="loaded",
                run_id=run_id,
            )
            log.info("loaded %s.%s (%d rows) from shop_oltp", d.raw_schema, d.raw_table, rows)
    analytics.commit()


def load_raw(
    repo_root: Path,
    settings: Settings,
    data_dir: Path,
    run_id: str,
    domain: str | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Load OLTP datasets, then drain each landing dataset's inbox through the contract gate.

    Returns the landing outcomes, e.g. {"loaded": 1, "quarantined": 0, "skipped": 0, ...}.
    """
    datasets = load_datasets(repo_root, domain)
    store = ObjectStore(settings.objectstore)
    outcomes = {"loaded": 0, "quarantined": 0, "skipped": 0, "waiting": 0}
    with db.connect(settings.analytics_loader) as analytics:
        ledger.ensure_schema(analytics)
        _load_oltp(analytics, settings, datasets, run_id)
        with tempfile.TemporaryDirectory() as tmp:
            for d in _landing_datasets(datasets):
                contract = load_contract_for(repo_root, d.domain, d.name)
                rules = LandingRules.from_contract(contract)
                if problems := rules.problems():
                    raise ValueError(f"{d.domain}.{d.name} landing contract: {'; '.join(problems)}")
                prefix = layout.inbox_prefix(d.source_system, d.name)
                for obj in store.list(store.inbox, prefix):
                    if obj.key.endswith("/"):
                        continue  # folder placeholder some SFTP/UI clients create
                    if not rules.is_settled(obj.last_modified, now or datetime.now(UTC)):
                        outcomes["waiting"] += 1
                        log.info("waiting on %s (completionSignal settle)", obj.key)
                        continue
                    pinned = store.head(obj.bucket, obj.key)
                    outcome = _process_delivery(
                        analytics, store, d, contract, rules, pinned, Path(tmp), run_id
                    )
                    outcomes[outcome] += 1
    log.info("landing outcomes: %s", outcomes)
    return outcomes


def _process_delivery(
    analytics: psycopg.Connection,
    store: ObjectStore,
    d: Dataset,
    contract: Contract | None,
    rules: LandingRules,
    obj: ObjectRef,
    tmp: Path,
    run_id: str,
) -> str:
    """Gate one pinned inbox object and file it to accepted/ or quarantine/ (ADR-0024).

    Crash-safe order: file to the archive -> load -> ledger commit -> delete the inbox version.
    A crash before the delete leaves the object in the inbox, and the next run redoes it.
    """
    dataset_id = f"{d.source_system}/{d.name}"
    common = {
        "source_type": "file",
        "source_identifier": obj.key,
        "object_etag": obj.etag,
        "object_version": obj.version_id,
        "contract_version": contract.version if contract else None,
        "target_schema": d.raw_schema,
        "target_table": d.raw_table,
        "run_id": run_id,
        "dataset": dataset_id,
    }

    try:
        partition = rules.partition_for(obj.filename, obj.last_modified)
        naming = None
    except FilenameMismatch as exc:
        # File the rejected delivery by arrival time so it can still be found and triaged.
        partition = rules.partition_from_arrival(obj.last_modified)
        naming = Assertion("naming.filename_pattern", None, False, str(exc))

    if naming is None and ledger.latest_loaded_etag(analytics, dataset_id) == obj.etag:
        ledger.record(
            analytics, **common, partition=partition, status="skipped",
            message="unchanged delivery: same checksum as the latest loaded file",
        )  # fmt: skip
        analytics.commit()
        store.delete_version(obj)
        log.info("skipped %s (same checksum as the latest load, etag %s)", obj.key, obj.etag)
        return "skipped"

    local = tmp / obj.filename
    if naming is not None:
        result = ValidationResult(passed=False, rows=0, assertions=[naming])
    else:
        store.download(obj, local)
        # Landing-zone quality gate: a file must match its ODCS contract before it may reach
        # the warehouse (proof P2). A dataset without a contract yet passes through.
        result = (
            validate_file(local, contract)
            if contract is not None
            else ValidationResult(passed=True, rows=0, assertions=[])
        )
        if rules.filename_pattern is not None:
            ok = Assertion("naming.filename_pattern", None, True, "file name matches the contract")
            result = ValidationResult(result.passed, result.rows, [ok, *result.assertions])
    checks = [asdict(a) for a in result.assertions]

    if not result.passed:
        key = layout.quarantine_key(d.source_system, d.name, partition, run_id, obj.filename)
        store.copy(obj, store.archive, key)
        store.put_json(
            store.archive,
            key.rsplit("/", 1)[0] + "/" + layout.REASON_FILE,
            {
                "dataset": dataset_id,
                "owner": d.domain,
                "contract": {"id": contract.id, "version": contract.version} if contract else None,
                "delivery": {
                    "bucket": obj.bucket,
                    "key": obj.key,
                    "version_id": obj.version_id,
                    "etag": obj.etag,
                    "size": obj.size,
                    "arrived_at": obj.last_modified,
                },  # fmt: skip
                "partition": partition,
                "run_id": run_id,
                "rows": result.rows,
                "failed": [c for c in checks if not c["passed"]],
                "checks": checks,
            },
        )
        ledger.record(
            analytics, **common, partition=partition, archive_key=key, checks=checks,
            row_count=result.rows, status="quarantined", message=result.summary(),
        )  # fmt: skip
        analytics.commit()
        store.delete_version(obj)
        log.warning(
            "QUARANTINED %s -> s3://%s/%s: %s", obj.key, store.archive, key, result.summary()
        )
        return "quarantined"

    key = layout.accepted_key(d.source_system, d.name, partition, obj.filename)
    store.copy(
        obj, store.archive, key,
        retain_until=rules.retain_until(datetime.now(UTC)), tagging=rules.retention_tag,
    )  # fmt: skip
    rows = db.load_csv_file(analytics, d.raw_schema, d.raw_table, local)
    ledger.record(
        analytics, **common, partition=partition, archive_key=key, checks=checks,
        row_count=rows, status="loaded",
    )  # fmt: skip
    analytics.commit()
    store.delete_version(obj)
    log.info("loaded %s.%s (%d rows) from %s", d.raw_schema, d.raw_table, rows, key)
    return "loaded"


# ---------------------------------------------------------------------- replay
def replay(
    repo_root: Path,
    settings: Settings,
    run_id: str,
    partition_date: str,
    domain: str | None = None,
) -> None:
    """Rebuild <domain>_raw from the sources of record: shop_oltp + the archive's accepted/ files.

    For each landing dataset the newest accepted file in partitions starting ``dt=<date>`` is
    loaded (it passed the gate when it arrived). Used by the restore drill (P9) and backfills;
    it never reads or drains the inbox.
    """
    datasets = load_datasets(repo_root, domain)
    store = ObjectStore(settings.objectstore)
    with db.connect(settings.analytics_loader) as analytics, tempfile.TemporaryDirectory() as tmp:
        ledger.ensure_schema(analytics)
        _load_oltp(analytics, settings, datasets, run_id)
        for d in _landing_datasets(datasets):
            prefix = layout.accepted_prefix(d.source_system, d.name) + f"dt={partition_date}"
            files = store.list(store.archive, prefix)
            if not files:
                raise FileNotFoundError(
                    f"no accepted file for {d.name} under s3://{store.archive}/{prefix}"
                )
            obj = store.head(store.archive, files[-1].key)
            local = Path(tmp) / obj.filename
            store.download(obj, local)
            rows = db.load_csv_file(analytics, d.raw_schema, d.raw_table, local)
            ledger.record(
                analytics,
                source_type="file",
                source_identifier=obj.key,
                object_etag=obj.etag,
                object_version=obj.version_id,
                target_schema=d.raw_schema,
                target_table=d.raw_table,
                row_count=rows,
                status="loaded",
                run_id=run_id,
                dataset=f"{d.source_system}/{d.name}",
                partition=obj.key[len(layout.accepted_prefix(d.source_system, d.name)) :].rsplit(
                    "/", 1
                )[0],
                archive_key=obj.key,
                message="replayed from archive",
            )
            analytics.commit()
            log.info("replayed %s.%s (%d rows) from %s", d.raw_schema, d.raw_table, rows, obj.key)
