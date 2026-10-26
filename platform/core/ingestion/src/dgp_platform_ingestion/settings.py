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

    @classmethod
    def analytics(cls, user: str, password: str) -> PostgresSettings:
        """The analytics warehouse, as ``user``. Inside the compose network the internal port
        applies (``ANALYTICS_INTERNAL_PORT``); from a laptop, the published one."""
        port = os.environ.get("ANALYTICS_INTERNAL_PORT") or os.environ.get("ANALYTICS_PORT", "5433")
        return cls(
            os.environ.get("ANALYTICS_HOST", "localhost"),
            int(port),
            os.environ.get("ANALYTICS_DB", "analytics"),
            user,
            password,
        )

    @classmethod
    def oltp(cls, user: str, password: str) -> PostgresSettings:
        """The operational source database, as ``user`` (ports as for ``analytics``)."""
        port = os.environ.get("OLTP_INTERNAL_PORT") or os.environ.get("OLTP_PORT", "5432")
        return cls(
            os.environ.get("OLTP_HOST", "localhost"),
            int(port),
            os.environ.get("OLTP_DB", "shop_oltp"),
            user,
            password,
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
    inbox_bucket: str  # partner-facing drops (ADR-0024)
    archive_bucket: str  # accepted evidence + quarantine, object-locked
    state_bucket: str
    # Object-lock mode for accepted files: GOVERNANCE locally (an admin can still clean up),
    # COMPLIANCE in production (nobody, root included, can delete before retain-until).
    lock_mode: str = "GOVERNANCE"

    def partner_credentials(self, source: str) -> tuple[str | None, str | None]:
        """Write-only credentials of a source's partner — used only by the PoC partner simulator
        (``land``) and proof P14. Real partners hold their own; the platform never does."""
        secret = os.environ.get(f"LANDING_PARTNER_{source.upper()}_SECRET")
        return (f"partner-{source}", secret) if secret else (None, None)


@dataclass(frozen=True)
class Settings:
    oltp_loader: PostgresSettings
    oltp_reader: PostgresSettings
    analytics_loader: PostgresSettings
    objectstore: ObjectStoreSettings
    kaggle_dataset: str
    # The landing arrival queue (ADR-0016); None when not configured (the gate then skips acks).
    landing_events: PostgresSettings | None = None

    @classmethod
    def from_env(cls) -> Settings:
        analytics, oltp = PostgresSettings.analytics, PostgresSettings.oltp
        return cls(
            oltp_loader=oltp("oltp_loader", _env("OLTP_LOADER_PASSWORD")),
            oltp_reader=oltp("oltp_reader", _env("OLTP_READER_PASSWORD")),
            analytics_loader=analytics("platform_loader", _env("PLATFORM_LOADER_PASSWORD")),
            objectstore=ObjectStoreSettings(
                # Empty MINIO_ENDPOINT selects real AWS S3; empty keys fall back to
                # boto3's default credential chain (ADR-0009, cloud portability).
                # The loader runs as the least-privilege landing-loader principal generated from
                # sources.yml (ADR-0024), never as the object-store root.
                endpoint_url=os.environ.get("MINIO_ENDPOINT", "http://localhost:9000") or None,
                access_key=os.environ.get("LANDING_LOADER_USER") or None,
                secret_key=os.environ.get("LANDING_LOADER_SECRET_KEY") or None,
                inbox_bucket=_env("LANDING_INBOX_BUCKET", "landing-inbox"),
                archive_bucket=_env("LANDING_ARCHIVE_BUCKET", "landing-archive"),
                state_bucket=_env("STATE_BUCKET", "platform-state"),
                lock_mode=os.environ.get("LANDING_LOCK_MODE", "GOVERNANCE"),
            ),
            kaggle_dataset=os.environ.get("OLIST_DATASET", "olistbr/brazilian-ecommerce"),
            landing_events=analytics("landing_events", os.environ["LANDING_EVENTS_PASSWORD"])
            if os.environ.get("LANDING_EVENTS_PASSWORD")
            else None,
        )
