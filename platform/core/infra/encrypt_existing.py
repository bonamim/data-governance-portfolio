#!/usr/bin/env python3
"""One-time migration: encrypt objects written before bucket encryption was enabled (X7).

``minio-init`` sets SSE-S3 as every bucket's default, which covers NEW writes. Objects that existed
before are rewritten in place (server-side copy onto themselves with SSE), so every current object
is encrypted at rest. The content is unchanged. A single-part object keeps its ETag (SSE-S3 keeps
the plaintext MD5), so the ledger still skips it (P6). A multipart-uploaded object gets a new
single-part ETag, so its next load runs once more. That is safe: raw loads replace the table.
Superseded versions written unencrypted expire with the bucket's noncurrent-version lifecycle.
Idempotent.

Run: ``make encrypt-at-rest``.
"""

from __future__ import annotations

import os
import sys

import boto3

BUCKETS = [
    os.environ.get("LANDING_INBOX_BUCKET", "landing-inbox"),
    os.environ.get("LANDING_ARCHIVE_BUCKET", "landing-archive"),
    os.environ.get("STATE_BUCKET", "platform-state"),
    os.environ.get("BACKUP_BUCKET", "platform-backups"),
]


def main() -> int:
    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ.get("MINIO_ENDPOINT", "http://localhost:9000") or None,
        aws_access_key_id=os.environ.get("MINIO_ROOT_USER") or None,
        aws_secret_access_key=os.environ.get("MINIO_ROOT_PASSWORD") or None,
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )
    rewritten = 0
    for bucket in BUCKETS:
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if s3.head_object(Bucket=bucket, Key=key).get("ServerSideEncryption"):
                    continue
                s3.copy_object(
                    Bucket=bucket,
                    Key=key,
                    CopySource={"Bucket": bucket, "Key": key},
                    ServerSideEncryption="AES256",
                    MetadataDirective="COPY",
                )
                rewritten += 1
                print(f"  encrypted s3://{bucket}/{key}")
    print(f"{rewritten} object(s) rewritten with SSE. Every current object is encrypted at rest.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
