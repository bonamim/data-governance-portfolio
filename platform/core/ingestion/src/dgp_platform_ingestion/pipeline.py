"""Ingestion orchestration: acquire → seed → land → gate → load-raw.

These functions are the self-serve platform capability. They are driven entirely
by the domains' ``sources.yml`` and know nothing dataset-specific, so a new domain
is onboarded with config alone (ADR-0008).

Flow for the M1 production slice:
  acquire   Download the Olist dataset from Kaggle into a local cache.
  seed      Load operational CSVs into shop_oltp (stands in for the source system).
  land      PoC partner simulator: PUT file-sourced CSVs into the landing inbox as the
            partner (write-only credentials, the partner's own file names).
  gate      Drain each landing inbox through the contract gate: every delivery is filed to
            accepted/ or quarantine/ and recorded in the ledger (ADR-0024). Loads nothing.
  load-raw  Load every dataset into <domain>_raw in analytics, recording the ingestion
            ledger. OLTP sources are read from shop_oltp, and landing datasets load the accepted/
            file the gate filed and no load has picked up yet (ADR-0025).
  replay    Rebuild raw from the sources of record: shop_oltp + the archive's accepted/
            files for a partition (restore drill, backfill). Never touches the inbox.
"""

from __future__ import annotations

import hashlib
import logging
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from dgp_platform_quality.engine import Assertion, ValidationResult, validate_file
from dgp_platform_quality.rules import contract_path, load_contract_for

from . import alerts, arrivals, db, governance, layout, ledger
from .config import Dataset, load_datasets, olist_files
from .layout import FilenameMismatch, LandingRules
from .objectstore import ObjectRef, ObjectStore
from .settings import Settings

log = logging.getLogger("dgp.ingestion")


def _today_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _oltp_datasets(datasets: list[Dataset]) -> list[tuple[Dataset, str]]:
    """OLTP datasets with their source table (config validation requires one)."""
    return [(d, d.oltp_table) for d in datasets if d.source_type == "oltp" and d.oltp_table]


def _landing_datasets(datasets: list[Dataset]) -> list[Dataset]:
    return [d for d in datasets if d.source_type == "landing"]


# --------------------------------------------------------------------- acquire
def acquire(repo_root: Path, settings: Settings, data_dir: Path) -> Path:
    """Download and unzip the Olist dataset, and return the cache directory."""
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
        for d, table in datasets:
            csv_path = cache / d.olist_file
            if not csv_path.exists():
                raise FileNotFoundError(f"{csv_path} missing. Run acquire first")
            rows = db.load_csv_file(conn, "shop", table, csv_path)
            log.info("seeded shop.%s (%d rows)", table, rows)
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
    production partners push on their own, so this function is not deployed (ADR-0016).
    """
    cache = data_dir / "olist"
    business_date = datetime.strptime(partition_date or _today_utc(), "%Y-%m-%d")
    for d in _landing_datasets(load_datasets(repo_root, domain)):
        csv_path = cache / d.olist_file
        if not csv_path.exists():
            raise FileNotFoundError(f"{csv_path} missing. Run acquire first")
        access, secret = settings.objectstore.partner_credentials(d.source_system)
        partner = ObjectStore(settings.objectstore, access, secret)
        key = layout.inbox_prefix(d.source_system, d.name) + d.delivered_name(business_date)
        partner.upload_file(csv_path, partner.inbox, key)
        log.info("partner %s delivered s3://%s/%s", d.source_system, partner.inbox, key)


# -------------------------------------------------------------------- gate
def contract_gate(
    repo_root: Path,
    settings: Settings,
    run_id: str,
    domain: str | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Drain each landing dataset's inbox through the contract gate (ADR-0016, -0024, -0025).

    Deliveries are decided in batches by the contract's completion signal: one file at a time
    (``per_file``, ``settle``) or a whole partition once complete (``expected_files``, ``marker``).
    Each batch is checked against its ODCS contract and filed to ``accepted/`` (ledger
    ``accepted``, waiting for ``load_raw``) or ``quarantine/`` (+ ``_reason.json``, and an alert to
    the owning domain). Nothing is written to the warehouse here. Processed arrivals are acked on
    the arrival queue. Returns file counts, e.g. {"accepted": 1, "quarantined": 0, "skipped": 0,
    "waiting": 0}.
    """
    store = ObjectStore(settings.objectstore)
    outcomes = {"accepted": 0, "quarantined": 0, "skipped": 0, "waiting": 0}
    datasets = _landing_datasets(load_datasets(repo_root, domain))
    with db.connect(settings.analytics_loader) as analytics, tempfile.TemporaryDirectory() as tmp:
        ledger.ensure_schema(analytics)
        for d in datasets:
            contract = load_contract_for(repo_root, d.domain, d.name)
            rules = LandingRules.from_contract(contract)
            if problems := rules.problems():
                raise ValueError(f"{d.domain}.{d.name} landing contract: {'; '.join(problems)}")
            prefix = layout.inbox_prefix(d.source_system, d.name)
            # Folder placeholders (some SFTP/UI clients create them) are not deliveries.
            objects = [o for o in store.list(store.inbox, prefix) if not o.key.endswith("/")]
            ready, waiting = layout.plan_batches(rules, objects, now or datetime.now(UTC))
            for b in waiting:
                outcomes["waiting"] += len(b.files) + (1 if b.marker else 0)
                log.info(
                    "waiting on %s %s (completionSignal %s)",
                    d.name,
                    b.partition or b.files[0].key,
                    rules.completion_signal,
                )
            for b in ready:
                files = [store.head(o.bucket, o.key) for o in b.files]
                gate = _Gate(analytics, store, repo_root, d, contract, rules, Path(tmp), run_id)
                outcomes[gate.decide(files, b.partition)] += len(files)
                if b.marker is not None:  # the partner's marker closed this batch, consumed
                    store.delete_version(store.head(b.marker.bucket, b.marker.key))
        _ack_arrivals(settings, store, datasets)
    log.info("gate outcomes: %s", outcomes)
    return outcomes


def _ack_arrivals(settings: Settings, store: ObjectStore, datasets: list[Dataset]) -> None:
    """Ack the arrival-queue rows of files that have left the inbox (ADR-0016)."""
    if settings.landing_events is None:
        log.info("no arrival queue configured (LANDING_EVENTS_PASSWORD unset): nothing to ack")
        return
    prefixes = [layout.inbox_prefix(d.source_system, d.name) for d in datasets]
    in_inbox = {o.key for p in prefixes for o in store.list(store.inbox, p)}
    with db.connect(settings.landing_events) as q:
        if acked := arrivals.ack(q, prefixes, in_inbox):
            log.info("acked %d arrival event(s)", acked)


@dataclass
class _Inspection:
    obj: ObjectRef
    partition: str
    local: Path | None
    sha256: str | None
    result: ValidationResult

    @property
    def checks(self) -> list[dict]:
        return [asdict(a) for a in self.result.assertions]


class _Gate:
    """Decide one batch: inspect every file, then file them all to accepted/ or quarantine/.

    Crash-safe order: file to the archive -> ledger commit -> delete the inbox versions. A crash
    before the commit leaves the files in the inbox and the next run redoes them. A crash after it
    is caught by the content check (``skipped``) and the inbox is drained.
    """

    def __init__(self, analytics, store, repo_root, d, contract, rules, tmp, run_id):
        self.analytics, self.store, self.repo_root = analytics, store, repo_root
        self.d, self.contract, self.rules, self.tmp, self.run_id = d, contract, rules, tmp, run_id
        self.dataset_id = f"{d.source_system}/{d.name}"

    def _common(self, i: _Inspection, batch_id: str) -> dict:
        return {
            "source_type": "file",
            "source_identifier": i.obj.key,
            "object_etag": i.obj.etag,
            "object_version": i.obj.version_id,
            "content_sha256": i.sha256,
            "arrived_at": i.obj.last_modified,
            "contract_version": self.contract.version if self.contract else None,
            "target_schema": self.d.raw_schema,
            "target_table": self.d.raw_table,
            "run_id": self.run_id,
            "dataset": self.dataset_id,
            "partition": i.partition,
            "batch_id": batch_id,
        }

    def inspect(self, obj: ObjectRef) -> _Inspection:
        rules = self.rules
        try:
            partition = rules.partition_for(obj.filename, obj.last_modified)
        except FilenameMismatch as exc:
            # File the rejected delivery by arrival time so it can still be found and triaged.
            naming = Assertion("naming.filename_pattern", None, False, str(exc))
            return _Inspection(
                obj,
                rules.partition_from_arrival(obj.last_modified),
                None,
                None,
                ValidationResult(passed=False, rows=0, assertions=[naming]),
            )
        local = self.tmp / obj.filename
        self.store.download(obj, local)
        sha = _sha256(local)
        # Landing-zone quality gate: a file must match its ODCS contract before it may reach
        # the warehouse (proof P2). A dataset without a contract yet passes through.
        result = (
            validate_file(local, self.contract)
            if self.contract is not None
            else ValidationResult(passed=True, rows=0, assertions=[])
        )
        extra = []
        if rules.filename_pattern is not None:
            extra.append(Assertion("naming.filename_pattern", None, True, "file name matches"))
            future = rules.in_future(partition, datetime.now(UTC))
            extra.append(
                Assertion(
                    "naming.business_date",
                    None,
                    not future,
                    f"business date {partition} is in the future"
                    if future
                    else "business date not in the future",
                )
            )
        # Integrity: a partner that sends x-amz-checksum-sha256 gets it verified (ADR-0016).
        if (sent := self.store.partner_checksum(obj)) is not None:
            ok = sent == sha
            extra.append(
                Assertion(
                    "integrity.checksum_sha256",
                    None,
                    ok,
                    "matches the partner's checksum"
                    if ok
                    else f"partner sent {sent[:12]}…, content is {sha[:12]}…",
                )
            )
        passed = result.passed and all(a.passed for a in extra)
        return _Inspection(
            obj,
            partition,
            local,
            sha,
            ValidationResult(passed, result.rows, [*extra, *result.assertions]),
        )

    def decide(self, files: list[ObjectRef], partition: str | None) -> str:
        found = [self.inspect(o) for o in files]
        batch_id = f"{self.run_id}:{partition or files[0].key}"
        if self._unchanged(found):
            for i in found:
                ledger.record(
                    self.analytics,
                    **self._common(i, batch_id),
                    status="skipped",
                    message="unchanged delivery: same content as the latest accepted",
                )
            self.analytics.commit()
            for i in found:
                self.store.delete_version(i.obj)
            log.info(
                "skipped %s (same content as the latest accepted)", ", ".join(f.key for f in files)
            )
            return "skipped"
        if all(i.result.passed for i in found):
            return self._accept(found, batch_id)
        return self._quarantine(found, batch_id, partition)

    def _unchanged(self, found: list[_Inspection]) -> bool:
        shas = [i.sha256 for i in found if i.sha256]
        if len(shas) < len(found):  # a file rejected on its name was never hashed
            return False
        if len(found) == 1:
            return ledger.unchanged(self.analytics, self.dataset_id, shas[0], found[0].obj.etag)
        return ledger.unchanged_batch(self.analytics, self.dataset_id, set(shas))

    def _accept(self, found: list[_Inspection], batch_id: str) -> str:
        for i in found:
            key = layout.accepted_key(
                self.d.source_system, self.d.name, i.partition, i.obj.filename
            )
            version = self.store.copy(
                i.obj,
                self.store.archive,
                key,
                retain_until=self.rules.retain_until(datetime.now(UTC)),
                tagging=self.rules.retention_tag,
            )
            ledger.record(
                self.analytics,
                **self._common(i, batch_id),
                archive_key=key,
                archive_version=version,
                checks=i.checks,
                row_count=i.result.rows,
                status="accepted",
            )
        self.analytics.commit()
        for i in found:
            self.store.delete_version(i.obj)
            log.info("ACCEPTED %s (waiting for load_raw)", i.obj.key)
        return "accepted"

    def _quarantine(self, found: list[_Inspection], batch_id: str, partition: str | None) -> str:
        d, store = self.d, self.store
        failing = [i for i in found if not i.result.passed]
        why = "; ".join(f"{i.obj.filename}: {i.result.summary()}" for i in failing)
        keys = {}
        for i in found:
            keys[i.obj.key] = layout.quarantine_key(
                d.source_system, d.name, i.partition, self.run_id, i.obj.filename
            )
            store.copy(i.obj, store.archive, keys[i.obj.key])
        for folder, members in _by_folder(found, keys).items():
            store.put_json(
                store.archive,
                f"{folder}/{layout.REASON_FILE}",
                {
                    "dataset": self.dataset_id,
                    "owner": d.domain,
                    "contract": {"id": self.contract.id, "version": self.contract.version}
                    if self.contract
                    else None,
                    "completion_signal": self.rules.completion_signal,
                    "batch": {"partition": partition, "files": [i.obj.filename for i in found]}
                    if len(found) > 1
                    else None,
                    "run_id": self.run_id,
                    "failed": [
                        {**c, "file": i.obj.filename}
                        for i in members
                        for c in i.checks
                        if not c["passed"]
                    ],
                    "files": [
                        {
                            "delivery": {
                                "bucket": i.obj.bucket,
                                "key": i.obj.key,
                                "version_id": i.obj.version_id,
                                "etag": i.obj.etag,
                                "size": i.obj.size,
                                "arrived_at": i.obj.last_modified,
                            },
                            "partition": i.partition,
                            "rows": i.result.rows,
                            "checks": i.checks,
                        }
                        for i in members
                    ],
                },
            )
        for i in found:
            msg = i.result.summary() if not i.result.passed else f"rejected with its batch: {why}"
            ledger.record(
                self.analytics,
                **self._common(i, batch_id),
                archive_key=keys[i.obj.key],
                checks=i.checks,
                row_count=i.result.rows,
                status="quarantined",
                message=msg,
            )
        self.analytics.commit()
        for i in found:
            store.delete_version(i.obj)
        log.warning("QUARANTINED %s: %s", ", ".join(i.obj.key for i in found), why)
        alerts.send(
            self.analytics,
            self.repo_root,
            domain=d.domain,
            kind="landing.quarantine",
            subject=f"{self.dataset_id}: {len(found)} file(s) quarantined: {why}"[:500],
            dedup_key=f"landing.quarantine:{self.dataset_id}:{batch_id}",
            detail={
                "files": [i.obj.key for i in found],
                "reason": f"s3://{store.archive}/{keys[found[0].obj.key].rsplit('/', 1)[0]}/"
                f"{layout.REASON_FILE}",
            },
        )
        return "quarantined"


def _by_folder(found: list[_Inspection], keys: dict[str, str]) -> dict[str, list[_Inspection]]:
    out: dict[str, list[_Inspection]] = {}
    for i in found:
        out.setdefault(keys[i.obj.key].rsplit("/", 1)[0], []).append(i)
    return out


# -------------------------------------------------------------------- load-raw
def _load_oltp(
    analytics: psycopg.Connection, repo_root: Path, settings: Settings, datasets, run_id: str
) -> None:
    """OLTP-sourced: stream shop_oltp -> temp CSV -> <domain>_raw. Each table is one snapshot: one
    batch, stamped with the table it was copied from and the day it was taken."""
    oltp_sets = _oltp_datasets(datasets)
    if not oltp_sets:
        return
    with db.connect(settings.oltp_reader) as oltp, tempfile.TemporaryDirectory() as tmp:
        for d, table in oltp_sets:
            dump = Path(tmp) / f"{table}.csv"
            db.export_table_to_csv(oltp, "shop", table, dump)
            batch_id = f"{run_id}:{d.raw_schema}.{d.raw_table}"
            rows = db.load_csv_file(
                analytics,
                d.raw_schema,
                d.raw_table,
                dump,
                governance=_raw_governance(analytics, repo_root, settings, d),
                system=governance.system_columns(settings.warehouse),
                stamp=_stamp(
                    batch_id,
                    f"postgres://{settings.oltp_reader.dbname}/shop.{table}",
                    f"dt={_today_utc()}",
                ),
            )
            ledger.record(
                analytics,
                source_type="table",
                source_identifier=f"shop_oltp.shop.{table}",
                target_schema=d.raw_schema,
                target_table=d.raw_table,
                row_count=rows,
                status="loaded",
                run_id=run_id,
                batch_id=batch_id,
            )
            log.info("loaded %s.%s (%d rows) from shop_oltp", d.raw_schema, d.raw_table, rows)
    analytics.commit()


def load_raw(
    repo_root: Path,
    settings: Settings,
    run_id: str,
    domain: str | None = None,
) -> dict[str, int]:
    """Load OLTP datasets, then each landing dataset's pending accepted file into <domain>_raw.

    Reads only what the contract gate already accepted (ledger rows no load has picked up), so a
    retry re-runs the load without re-checking any file (ADR-0025). Raw holds one batch per dataset
    (a file, or a partition's files under a batch completion signal): when several are pending, the
    newest is loaded and the older ones are recorded ``superseded``.
    Returns the landing outcomes, e.g. {"loaded": 1, "superseded": 0}.
    """
    datasets = load_datasets(repo_root, domain)
    store = ObjectStore(settings.objectstore)
    outcomes = {"loaded": 0, "superseded": 0}
    with db.connect(settings.analytics_loader) as analytics, tempfile.TemporaryDirectory() as tmp:
        ledger.ensure_schema(analytics)
        _load_oltp(analytics, repo_root, settings, datasets, run_id)
        for d in _landing_datasets(datasets):
            dataset_id = f"{d.source_system}/{d.name}"
            batches = ledger.pending_batches(analytics, dataset_id)
            if not batches:
                log.info("nothing accepted for %s since the last load", dataset_id)
                continue
            *older, newest = batches
            common = {
                "source_type": "file",
                "target_schema": d.raw_schema,
                "target_table": d.raw_table,
                "run_id": run_id,
                "dataset": dataset_id,
            }
            # The newest batch (one file, or a whole partition) replaces raw: the first file
            # recreates the table, the rest append. Load and ledger rows share one transaction:
            # either raw has the batch and the ledger says so, or neither (a retry loads it again).
            total, loaded_at = 0, datetime.now(UTC)
            for n, a in enumerate(newest):
                obj = store.head(store.archive, a.archive_key, a.archive_version)
                local = Path(tmp) / obj.filename
                store.download(obj, local)
                rows = db.load_csv_file(
                    analytics,
                    d.raw_schema,
                    d.raw_table,
                    local,
                    append=n > 0,
                    governance=_raw_governance(analytics, repo_root, settings, d),
                    system=governance.system_columns(settings.warehouse),
                    stamp=_stamp(
                        a.batch_id, f"s3://{store.archive}/{a.archive_key}", a.partition, loaded_at
                    ),
                )
                ledger.record(analytics, **common, **_from_gate(a), row_count=rows, status="loaded")
                total += rows
            for a in (a for b in older for a in b):
                ledger.record(
                    analytics,
                    **common,
                    **_from_gate(a),
                    status="superseded",
                    message=f"a newer accepted batch was loaded instead ({newest[0].batch_id})",
                )
            analytics.commit()
            outcomes["loaded"] += len(newest)
            outcomes["superseded"] += sum(len(b) for b in older)
            log.info(
                "loaded %s.%s (%d rows) from %d file(s)",
                d.raw_schema,
                d.raw_table,
                total,
                len(newest),
            )
    log.info("load outcomes: %s", outcomes)
    return outcomes


def _from_gate(a: ledger.Accepted) -> dict:
    """The delivery identity a load row carries over from its gate row."""
    return {
        "gate_id": a.id,
        "source_identifier": a.source_identifier,
        "object_etag": a.object_etag,
        "content_sha256": a.content_sha256,
        "arrived_at": a.arrived_at,
        "batch_id": a.batch_id,
        "object_version": a.object_version,
        "contract_version": a.contract_version,
        "partition": a.partition,
        "archive_key": a.archive_key,
        "archive_version": a.archive_version,
    }


def _raw_governance(
    analytics: psycopg.Connection, repo_root: Path, settings: Settings, d: Dataset
) -> Callable[[], list[str]]:
    """What describes and tags the raw table (governance.py): its landing contract when it has one,
    and the platform's system columns. Rendered once the table exists (``db.load_csv_file`` runs
    it before the first row is written)."""

    def render() -> list[str]:
        path = contract_path(repo_root, d.domain, d.name)
        gov = governance.RawGovernance.from_contract(path if path.exists() else None)
        present = db.columns_of(analytics, d.raw_schema, d.raw_table)
        return governance.statements(settings.warehouse, d.raw_schema, d.raw_table, gov, present)

    return render


def _stamp(
    batch_id: str, source_path: str, partition: str | None, loaded_at: datetime | None = None
) -> dict[str, object]:
    """The platform's system columns for the rows of one delivery (ADR-0028). Every file of one
    batch passes the batch's ``loaded_at``, so all its rows share it."""
    return {
        "_system_batch_id": batch_id,
        "_system_source_path": source_path,
        "_system_partition": partition,
        "_system_loaded_at": loaded_at or datetime.now(UTC),
    }


# ---------------------------------------------------------------------- replay
def replay(
    repo_root: Path,
    settings: Settings,
    run_id: str,
    partition_date: str,
    domain: str | None = None,
) -> None:
    """Rebuild <domain>_raw from the sources of record: shop_oltp + the archive's accepted/ files.

    For each landing dataset the newest accepted file on or before ``partition_date`` is loaded (it
    passed the gate when it arrived): datasets deliver on their own cadence, so one date need not
    have a file for every dataset. Used by the restore drill (P9) and backfills. It never reads or
    drains the inbox.
    """
    datasets = load_datasets(repo_root, domain)
    store = ObjectStore(settings.objectstore)
    with db.connect(settings.analytics_loader) as analytics, tempfile.TemporaryDirectory() as tmp:
        ledger.ensure_schema(analytics)
        _load_oltp(analytics, repo_root, settings, datasets, run_id)
        for d in _landing_datasets(datasets):
            prefix = layout.accepted_prefix(d.source_system, d.name)
            keys = [o.key for o in store.list(store.archive, prefix)]
            key = layout.newest_as_of(d.source_system, d.name, keys, partition_date)
            if key is None:
                raise FileNotFoundError(
                    f"no accepted file for {d.name} on or before {partition_date} "
                    f"under s3://{store.archive}/{prefix}"
                )
            obj = store.head(store.archive, key)
            local = Path(tmp) / obj.filename
            store.download(obj, local)
            batch_id = f"{run_id}:{obj.key}"
            partition = layout.accepted_partition(d.source_system, d.name, obj.key)
            rows = db.load_csv_file(
                analytics,
                d.raw_schema,
                d.raw_table,
                local,
                governance=_raw_governance(analytics, repo_root, settings, d),
                system=governance.system_columns(settings.warehouse),
                stamp=_stamp(batch_id, f"s3://{store.archive}/{obj.key}", partition),
            )
            ledger.record(
                analytics,
                source_type="file",
                source_identifier=obj.key,
                object_etag=obj.etag,
                content_sha256=_sha256(local),
                object_version=obj.version_id,
                target_schema=d.raw_schema,
                target_table=d.raw_table,
                row_count=rows,
                status="loaded",
                run_id=run_id,
                dataset=f"{d.source_system}/{d.name}",
                partition=partition,
                archive_key=obj.key,
                message="replayed from archive",
                batch_id=batch_id,
            )
            analytics.commit()
            log.info("replayed %s.%s (%d rows) from %s", d.raw_schema, d.raw_table, rows, obj.key)
