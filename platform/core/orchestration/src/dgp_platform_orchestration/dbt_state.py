"""dbt state store (ADR-0015 §3): the published manifest of each domain, in ``platform-state``.

After a domain publishes successfully its ``target/manifest.json`` is saved to
``s3://<STATE_BUCKET>/dbt/<domain>/manifest.json``. Slim CI reads it back as ``--state`` to build
only what a change touched (``state:modified+``) and ``--defer`` the rest to what is published.
Written as the ``dbt-state`` principal (read/write on ``dbt/*`` only), never the store's root.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from botocore.exceptions import ClientError
from dgp_platform_ingestion.objectstore import ObjectStore
from dgp_platform_ingestion.settings import Settings

log = logging.getLogger("dgp.dbt_state")


def _store() -> ObjectStore:
    secret = os.environ.get("DBT_STATE_SECRET_KEY")
    if not secret:
        raise RuntimeError("DBT_STATE_SECRET_KEY is not set — the dbt state principal is needed")
    user = os.environ.get("DBT_STATE_USER", "dbt-state")
    return ObjectStore(Settings.from_env().objectstore, user, secret)


def key(domain: str) -> str:
    return f"dbt/{domain}/manifest.json"


def save(manifest: Path, domain: str) -> str:
    """Publish a domain's manifest as the state baseline; returns its s3:// URI."""
    store = _store()
    bucket = store.settings.state_bucket
    store.upload_file(manifest, bucket, key(domain))
    uri = f"s3://{bucket}/{key(domain)}"
    log.info("saved dbt state for %s -> %s", domain, uri)
    return uri


def fetch(domain: str, dest_dir: Path) -> Path | None:
    """Download the published manifest into ``dest_dir``; None if there is no baseline yet."""
    store = _store()
    bucket = store.settings.state_bucket
    try:
        ref = store.head(bucket, key(domain))
    except ClientError:
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / "manifest.json"
    store.download(ref, out)
    return out
