"""Ingestion orchestration: acquire → seed → land → load-raw.

These functions are the self-serve platform capability. They are driven entirely
by the domains' ``sources.yml`` and know nothing dataset-specific, so a new domain
is onboarded with config alone (ADR-0008).

Flow for the M1 production slice:
  acquire   Download the Olist dataset from Kaggle into a local cache.
  seed      Load operational CSVs into shop_oltp (stands in for the source system).
  land      Upload file-sourced CSVs to the MinIO landing zone (versioned).
  load-raw  Load every dataset into <domain>_raw in analytics, recording the
            ingestion ledger. OLTP sources are read from shop_oltp; landing
            sources are read back from MinIO (idempotent by ETag).
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from . import db, ledger
from .config import Dataset, load_datasets, olist_files
from .objectstore import ObjectStore
from .settings import Settings

log = logging.getLogger("dgp.ingestion")


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
    repo_root: Path, settings: Settings, data_dir: Path, domain: str | None = None
) -> None:
    """Upload file-sourced CSVs to the landing zone (versioned bucket)."""
    cache = data_dir / "olist"
    store = ObjectStore(settings.objectstore)
    for d in _landing_datasets(load_datasets(repo_root, domain)):
        csv_path = cache / d.olist_file
        if not csv_path.exists():
            raise FileNotFoundError(f"{csv_path} missing — run acquire first")
        etag = store.upload_file(csv_path, d.landing_key)
        log.info(
            "landed %s -> s3://%s/%s (etag %s)",
            d.olist_file,
            store.landing_bucket,
            d.landing_key,
            etag,
        )


# -------------------------------------------------------------------- load-raw
def load_raw(
    repo_root: Path, settings: Settings, data_dir: Path, run_id: str, domain: str | None = None
) -> None:
    """Load every dataset into <domain>_raw, recording the ingestion ledger."""
    datasets = load_datasets(repo_root, domain)
    store = ObjectStore(settings.objectstore)

    with db.connect(settings.analytics_loader) as analytics:
        # OLTP-sourced: stream shop_oltp -> temp CSV -> <domain>_raw.
        oltp_sets = _oltp_datasets(datasets)
        if oltp_sets:
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
                    log.info(
                        "loaded %s.%s (%d rows) from shop_oltp", d.raw_schema, d.raw_table, rows
                    )

        # Landing-sourced: read back from MinIO, skip if the same ETag already loaded.
        with tempfile.TemporaryDirectory() as tmp:
            for d in _landing_datasets(datasets):
                etag = store.etag(d.landing_key)
                if ledger.file_already_loaded(analytics, d.landing_key, etag):
                    ledger.record(
                        analytics,
                        source_type="file",
                        source_identifier=d.landing_key,
                        object_etag=etag,
                        target_schema=d.raw_schema,
                        target_table=d.raw_table,
                        status="skipped",
                        run_id=run_id,
                        message="unchanged file (same ETag) already loaded",
                    )
                    log.info("skipped %s (unchanged, etag %s)", d.landing_key, etag)
                    continue
                local = Path(tmp) / d.olist_file
                store.download_to(d.landing_key, local)
                rows = db.load_csv_file(analytics, d.raw_schema, d.raw_table, local)
                ledger.record(
                    analytics,
                    source_type="file",
                    source_identifier=d.landing_key,
                    object_etag=etag,
                    target_schema=d.raw_schema,
                    target_table=d.raw_table,
                    row_count=rows,
                    status="loaded",
                    run_id=run_id,
                )
                log.info("loaded %s.%s (%d rows) from landing", d.raw_schema, d.raw_table, rows)

        analytics.commit()
