"""Connection settings, read from the environment (the .env file locally).

Hosts default to localhost so the CLI works from a laptop against the compose
ports. Inside the Airflow containers the same variables are overridden to the
compose service names (postgres-oltp, postgres-analytics, minio).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"required environment variable {name} is not set")
    return value


@dataclass(frozen=True)
class PostgresSettings:
    host: str
    port: int
    dbname: str
    user: str
    password: str

    def conninfo(self) -> str:
        return (
            f"host={self.host} port={self.port} dbname={self.dbname} "
            f"user={self.user} password={self.password}"
        )


@dataclass(frozen=True)
class ObjectStoreSettings:
    # endpoint_url set -> MinIO (or any S3-compatible store); None -> real AWS S3.
    # access/secret None -> use boto3's default credential chain (IAM role, env, etc.).
    # This is the whole cloud-portability seam: local uses MinIO, cloud leaves
    # MINIO_ENDPOINT empty and the same code talks to S3 (ADR-0009).
    endpoint_url: str | None
    access_key: str | None
    secret_key: str | None
    landing_bucket: str
    state_bucket: str


@dataclass(frozen=True)
class Settings:
    oltp_loader: PostgresSettings
    oltp_reader: PostgresSettings
    analytics_loader: PostgresSettings
    objectstore: ObjectStoreSettings
    kaggle_dataset: str

    @classmethod
    def from_env(cls) -> Settings:
        oltp_host = os.environ.get("OLTP_HOST", "localhost")
        oltp_port = int(os.environ.get("OLTP_INTERNAL_PORT", os.environ.get("OLTP_PORT", "5432")))
        oltp_db = _env("OLTP_DB", "shop_oltp")

        ana_host = os.environ.get("ANALYTICS_HOST", "localhost")
        ana_port = int(
            os.environ.get("ANALYTICS_INTERNAL_PORT", os.environ.get("ANALYTICS_PORT", "5433"))
        )
        ana_db = _env("ANALYTICS_DB", "analytics")

        return cls(
            oltp_loader=PostgresSettings(
                oltp_host, oltp_port, oltp_db, "oltp_loader", _env("OLTP_LOADER_PASSWORD")
            ),
            oltp_reader=PostgresSettings(
                oltp_host, oltp_port, oltp_db, "oltp_reader", _env("OLTP_READER_PASSWORD")
            ),
            analytics_loader=PostgresSettings(
                ana_host, ana_port, ana_db, "platform_loader", _env("PLATFORM_LOADER_PASSWORD")
            ),
            objectstore=ObjectStoreSettings(
                # Empty MINIO_ENDPOINT selects real AWS S3; empty keys fall back to
                # boto3's default credential chain (ADR-0009, cloud portability).
                endpoint_url=os.environ.get("MINIO_ENDPOINT", "http://localhost:9000") or None,
                access_key=os.environ.get("MINIO_ROOT_USER") or None,
                secret_key=os.environ.get("MINIO_ROOT_PASSWORD") or None,
                landing_bucket=_env("LANDING_BUCKET", "landing"),
                state_bucket=_env("STATE_BUCKET", "platform-state"),
            ),
            kaggle_dataset=os.environ.get("OLIST_DATASET", "olistbr/brazilian-ecommerce"),
        )
