"""Shared helpers for the backup job and the restore drill (proof P9, docs/disaster-recovery.md).

Backups go to the ``platform-backups`` bucket (``BACKUP_BUCKET``), versioned, and in production a
DIFFERENT, object-locked, replicated location from the landing zone (DR §3). Each run writes
``backups/<UTC timestamp>/`` holding one file per store plus a ``manifest.json`` with each file's
sha256 and the row counts the drill must reproduce after restoring.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import boto3
from botocore.client import BaseClient
from dgp_governance import products

BACKUP_BUCKET = os.environ.get("BACKUP_BUCKET", "platform-backups")
BACKUP_PREFIX = "backups/"
DRILL_PREFIX = "drills/"

# Recovery point objective for the tier-1 OLTP source (DR §1): max acceptable data loss.
RPO_SECONDS = {"shop_oltp": 5 * 60}

# Recovery objectives (docs/disaster-recovery.md §1), in seconds. The drill measures against these.
RTO_SECONDS = {
    "shop_oltp": 1 * 3600,
    "datahub": 8 * 3600,
    "airflow": 4 * 3600,
    "ingestion_ledger": 4 * 3600,
    "superset": 24 * 3600,
    "analytics": 8 * 3600,
}


def s3() -> BaseClient:
    """S3 client: MinIO locally, real S3 when MINIO_ENDPOINT is empty (ADR-0009)."""
    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("MINIO_ENDPOINT", "http://localhost:9000") or None,
        aws_access_key_id=os.environ.get("MINIO_ROOT_USER") or None,
        aws_secret_access_key=os.environ.get("MINIO_ROOT_PASSWORD") or None,
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )


def container(project: str, service: str) -> str | None:
    """Running container id of a compose service, or None if it is not up."""
    out = subprocess.run(
        [
            "docker",
            "ps",
            "-q",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            f"label=com.docker.compose.service={service}",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return out[0] if out else None


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def output_ports() -> list[str]:
    """Every governed output port as ``schema.table`` (from the data product definitions)."""
    locations = (products.location(p.owner, p.name) for p in products.data_products() if p.owner)
    return [".".join(loc) for loc in locations if loc]


def latest_manifest(client: BaseClient) -> tuple[str, dict]:
    """(prefix, manifest) of the newest backup run in the bucket."""
    keys: list[str] = []
    for page in client.get_paginator("list_objects_v2").paginate(
        Bucket=BACKUP_BUCKET, Prefix=BACKUP_PREFIX
    ):
        keys += [o["Key"] for o in page.get("Contents", []) if o["Key"].endswith("manifest.json")]
    if not keys:
        raise SystemExit(
            f"no backup found in s3://{BACKUP_BUCKET}/{BACKUP_PREFIX}. Run make backup"
        )
    key = max(keys)  # prefixes are sortable UTC timestamps
    body = client.get_object(Bucket=BACKUP_BUCKET, Key=key)["Body"].read()
    return key.rsplit("/", 1)[0] + "/", json.loads(body)
