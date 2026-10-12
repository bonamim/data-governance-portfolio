"""MinIO (S3) access for the landing zone, via boto3.

Only the thin operations the platform needs: put a file, head (for the ETag used
as the ingestion-ledger checksum), and get an object as a stream.
"""

from __future__ import annotations

import os
from pathlib import Path

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from .settings import ObjectStoreSettings


class ObjectStore:
    def __init__(self, settings: ObjectStoreSettings):
        self._settings = settings
        # Cloud-portable (ADR-0009): with an endpoint set we talk to MinIO using
        # path-style addressing; with it unset we talk to real AWS S3 using its
        # defaults. Omitting keys lets boto3 use its default credential chain
        # (IAM role, instance profile, env) — the norm in a cloud deployment.
        kwargs: dict = {"config": Config(signature_version="s3v4")}
        if settings.endpoint_url:
            kwargs["endpoint_url"] = settings.endpoint_url
            kwargs["region_name"] = os.environ.get("AWS_REGION", "us-east-1")
            kwargs["config"] = Config(signature_version="s3v4", s3={"addressing_style": "path"})
        if settings.access_key and settings.secret_key:
            kwargs["aws_access_key_id"] = settings.access_key
            kwargs["aws_secret_access_key"] = settings.secret_key
        self._client = boto3.client("s3", **kwargs)

    @property
    def landing_bucket(self) -> str:
        return self._settings.landing_bucket

    def upload_file(self, local_path: Path, key: str) -> str:
        """Upload a file to the landing bucket; return its ETag."""
        self._client.upload_file(str(local_path), self.landing_bucket, key)
        return self.etag(key)

    def etag(self, key: str) -> str:
        try:
            head = self._client.head_object(Bucket=self.landing_bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
                raise FileNotFoundError(
                    f"landing object '{key}' not found in bucket "
                    f"'{self.landing_bucket}' — run 'dgp-ingest land' first"
                ) from exc
            raise
        return head["ETag"].strip('"')

    def download_to(self, key: str, dest: Path) -> None:
        self._client.download_file(self.landing_bucket, key, str(dest))
