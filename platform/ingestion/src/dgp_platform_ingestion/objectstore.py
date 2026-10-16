"""Object-store (MinIO / S3) access for the landing zone, via boto3.

Only the thin operations the platform needs: list a prefix, head and download a *pinned*
object version, upload, copy across buckets (with object lock + retention tags for accepted
files), write a small JSON object, and delete one version. Buckets are explicit per call: the
landing zone is two buckets (inbox + archive, ADR-0024).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.client import Config

from .settings import ObjectStoreSettings


@dataclass(frozen=True)
class ObjectRef:
    bucket: str
    key: str
    version_id: str | None
    etag: str
    last_modified: datetime
    size: int

    @property
    def filename(self) -> str:
        return self.key.rsplit("/", 1)[-1]


class ObjectStore:
    def __init__(
        self,
        settings: ObjectStoreSettings,
        access_key: str | None = None,
        secret_key: str | None = None,
    ):
        """Connect as the landing loader (default) or as another principal (e.g. a partner)."""
        self.settings = settings
        access_key = access_key or settings.access_key
        secret_key = secret_key or settings.secret_key
        # Cloud-portable (ADR-0009): with an endpoint set we talk to MinIO using
        # path-style addressing; with it unset we talk to real AWS S3 using its
        # defaults. Omitting keys lets boto3 use its default credential chain
        # (IAM role, instance profile, env) — the norm in a cloud deployment.
        kwargs: dict = {"config": Config(signature_version="s3v4")}
        if settings.endpoint_url:
            kwargs["endpoint_url"] = settings.endpoint_url
            kwargs["region_name"] = os.environ.get("AWS_REGION", "us-east-1")
            kwargs["config"] = Config(signature_version="s3v4", s3={"addressing_style": "path"})
        if access_key and secret_key:
            kwargs["aws_access_key_id"] = access_key
            kwargs["aws_secret_access_key"] = secret_key
        self.client = boto3.client("s3", **kwargs)

    @property
    def inbox(self) -> str:
        return self.settings.inbox_bucket

    @property
    def archive(self) -> str:
        return self.settings.archive_bucket

    def list(self, bucket: str, prefix: str) -> list[ObjectRef]:
        """Current objects under a prefix, oldest first (version ids resolved by ``head``)."""
        out = []
        for page in self.client.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=prefix
        ):
            for o in page.get("Contents", []):
                out.append(
                    ObjectRef(
                        bucket, o["Key"], None, o["ETag"].strip('"'), o["LastModified"], o["Size"]
                    )
                )
        return sorted(out, key=lambda r: (r.last_modified, r.key))

    def head(self, bucket: str, key: str, version_id: str | None = None) -> ObjectRef:
        """Pin the current version of ``key``, or the given one (an archived accepted file)."""
        kwargs = {"Bucket": bucket, "Key": key}
        if version_id:
            kwargs["VersionId"] = version_id
        h = self.client.head_object(**kwargs)
        return ObjectRef(
            bucket,
            key,
            h.get("VersionId"),
            h["ETag"].strip('"'),
            h["LastModified"],
            h["ContentLength"],
        )

    def upload_file(self, local_path: Path, bucket: str, key: str) -> None:
        """Upload a file (the partner simulator). No read-back: a partner is write-only."""
        self.client.upload_file(str(local_path), bucket, key)

    def download(self, ref: ObjectRef, dest: Path) -> None:
        """Download exactly the version that was inspected, even if the partner overwrote it."""
        extra = {"VersionId": ref.version_id} if ref.version_id else None
        self.client.download_file(ref.bucket, ref.key, str(dest), ExtraArgs=extra)

    def copy(
        self,
        src: ObjectRef,
        bucket: str,
        key: str,
        *,
        retain_until: datetime | None = None,
        tagging: str | None = None,
    ) -> str | None:
        """Server-side copy of a pinned version into ``bucket/key``; returns the new version id.

        With ``retain_until`` the copy is object-locked (mode from settings) — accepted evidence.
        Managed transfer, so files above the 5 GB single-copy limit work too.
        """
        source = {"Bucket": src.bucket, "Key": src.key}
        if src.version_id:
            source["VersionId"] = src.version_id
        extra: dict = {}
        if retain_until is not None:
            extra["ObjectLockMode"] = self.settings.lock_mode
            extra["ObjectLockRetainUntilDate"] = retain_until
        if tagging:
            extra["Tagging"] = tagging
            extra["TaggingDirective"] = "REPLACE"
        # Multipart copy for large objects; lock + tags apply to the completed object.
        self.client.copy(source, bucket, key, ExtraArgs=extra or None, Config=TransferConfig())
        return self.client.head_object(Bucket=bucket, Key=key).get("VersionId")

    def put_json(self, bucket: str, key: str, doc: dict) -> None:
        self.client.put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(doc, indent=2, default=str).encode(),
            ContentType="application/json",
        )

    def delete_version(self, ref: ObjectRef) -> None:
        """Remove the processed version from the inbox (a newer re-send, if any, stays)."""
        kwargs = {"Bucket": ref.bucket, "Key": ref.key}
        if ref.version_id:
            kwargs["VersionId"] = ref.version_id
        self.client.delete_object(**kwargs)
