"""What every proof shares: how it reports a check and its verdict, and how it connects."""

from __future__ import annotations

import os

import boto3
import psycopg
from botocore.client import BaseClient
from dgp_platform_ingestion.settings import PostgresSettings


class Checks:
    """The checks one proof makes. Call it with each check's outcome: the line is printed as the
    proof runs, and the failures are kept for the verdict."""

    def __init__(self, proof: str) -> None:
        self.proof = proof
        self.failures: list[str] = []

    def __call__(self, ok: bool, what: str, detail: str = "") -> bool:
        print(f"  {'OK  ' if ok else 'FAIL'} {what}" + (f" — {detail}" if detail else ""))
        if not ok:
            self.failures.append(what)
        return ok

    def verdict(self, proven: str) -> int:
        """Print what the proof showed — or which checks failed — and return the exit code."""
        if self.failures:
            print(
                f"\n{self.proof} FAILED: {len(self.failures)} check(s): {', '.join(self.failures)}"
            )
            return 1
        print(f"\n{self.proof} proven: {proven}")
        return 0


def warehouse(user: str, password_env: str, *, autocommit: bool = False) -> psycopg.Connection:
    """A connection to the warehouse as ``user``, whose password is in ``password_env``."""
    settings = PostgresSettings.analytics(user, os.environ[password_env])
    return psycopg.connect(settings.conninfo(), autocommit=autocommit)


def object_store(access_key: str | None, secret_key: str | None) -> BaseClient:
    """An S3 client for MinIO as one principal — a proof checks what each principal may do."""
    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("MINIO_ENDPOINT", "http://localhost:9000") or None,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
    )
