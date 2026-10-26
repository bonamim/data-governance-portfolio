#!/usr/bin/env python3
"""Proof P13 — production hardening controls, verified on the running platform (M8).

Every control that can be proven on a single machine is asserted here, against the live stack:

  X3  TLS        plain TCP to Postgres is REFUSED; verify-full against the local CA succeeds with
                 TLS >= 1.2; every open network session on the warehouse is encrypted
  X5  least      the admin, catalog and agent roles are not superusers and cannot create roles;
      privilege  catalog_reader can read metadata but cannot write; the admin plane owns the masking
                 views (not postgres)
  X9  audit      connection, disconnection and DDL logging are on, and this proof's own connection
      logging    appears in the server log
  X7  encryption every bucket defaults to SSE and every current object is encrypted
  X2  defaults   the production preflight REFUSES the shipped dev credentials
  X8  pinning    every image and GitHub Action is pinned (digest / commit SHA)
  X10 DR alerts  backups are fresh and a restore drill passed recently

Deterministic. Needs the live platform (`make up`, `make certs`). Run: ``make prove-p13``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid

import psycopg
from _checks import Checks, object_store
from dgp_governance.repo import root
from psycopg import errors

REPO = root()
CA = os.environ.get(
    "PGSSLROOTCERT", str(REPO / "platform" / "core" / "infra" / "tls" / "out" / "ca.crt")
)
ANALYTICS = (
    f"host=localhost port={os.environ.get('ANALYTICS_PORT', '5433')} "
    f"dbname={os.environ.get('ANALYTICS_DB', 'analytics')}"
)
OLTP = (
    f"host=localhost port={os.environ.get('OLTP_PORT', '5432')} "
    f"dbname={os.environ.get('OLTP_DB', 'shop_oltp')}"
)
ADMIN_ROLES = ["access_admin", "catalog_reader", "analytics_reader", "compliance"]
check = Checks("P13")


def connect(
    base: str, user: str, pw_env: str, mode: str = "verify-full", app: str = "p13"
) -> psycopg.Connection:
    return psycopg.connect(
        f"{base} user={user} password={os.environ[pw_env]} sslmode={mode} sslrootcert={CA} "
        f"application_name={app}"
    )


def tls() -> None:
    print("X3 — TLS")
    for name, base, user, pw in (
        ("analytics", ANALYTICS, "analytics_reader", "ANALYTICS_READER_PASSWORD"),
        ("shop_oltp", OLTP, "catalog_reader", "CATALOG_READER_PASSWORD"),
    ):
        try:
            connect(base, user, pw, mode="disable").close()
            check(False, f"{name}: plain TCP refused")
        except psycopg.OperationalError as exc:
            check(
                "no encryption" in str(exc),
                f"{name}: plain TCP refused",
                "pg_hba: hostnossl reject",
            )
        with connect(base, user, pw) as c:
            ssl, version = c.execute(
                "SELECT ssl, version FROM pg_stat_ssl WHERE pid = pg_backend_pid()"
            ).fetchone()
        check(ssl and version in ("TLSv1.2", "TLSv1.3"), f"{name}: verify-full over TLS", version)
    with connect(ANALYTICS, "access_admin", "ACCESS_ADMIN_PASSWORD") as c:
        plain = c.execute(
            "SELECT count(*) FROM pg_stat_activity a JOIN pg_stat_ssl s USING (pid) "
            "WHERE a.client_addr IS NOT NULL AND NOT s.ssl"
        ).fetchone()[0]
        total = c.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE client_addr IS NOT NULL"
        ).fetchone()[0]
    check(plain == 0, "analytics: every network session is encrypted", f"{total} sessions, 0 plain")


def least_privilege() -> None:
    print("X5 — least privilege")
    with connect(ANALYTICS, "access_admin", "ACCESS_ADMIN_PASSWORD") as c:
        rows = c.execute(
            "SELECT rolname, rolsuper, rolcreaterole, rolcreatedb FROM pg_roles "
            "WHERE rolname = ANY(%s) OR rolname LIKE 'agent\\_%%'",
            (ADMIN_ROLES,),
        ).fetchall()
        powerful = [r[0] for r in rows if any(r[1:])]
        check(
            not powerful and len(rows) >= len(ADMIN_ROLES) + 1,
            "admin/catalog/consumer/agent roles hold no superuser, CREATEROLE or CREATEDB",
            f"{len(rows)} roles",
        )
        owners = {
            r[0]
            for r in c.execute(
                "SELECT viewowner FROM pg_views WHERE schemaname LIKE '%%\\_masked'"
            ).fetchall()
        }
        check(
            owners == {"access_admin"},
            "masking views owned by access_admin, not the superuser",
            str(sorted(owners)),
        )
    with connect(ANALYTICS, "catalog_reader", "CATALOG_READER_PASSWORD") as c:
        seen = c.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema LIKE '%%\\_raw'"
        ).fetchone()[0]
        check(seen > 0, "catalog_reader sees the warehouse metadata", f"{seen} raw tables")
        try:
            c.execute("DELETE FROM platform_meta.ingestion_ledger WHERE false")
            check(False, "catalog_reader cannot write")
        except errors.InsufficientPrivilege:
            c.rollback()
            check(True, "catalog_reader cannot write", "DELETE denied")


def audit_logging() -> None:
    print("X9 — audit logging")
    marker = f"p13-{uuid.uuid4().hex[:8]}"
    with connect(ANALYTICS, "analytics_reader", "ANALYTICS_READER_PASSWORD", app=marker) as c:
        settings = {
            k: c.execute(f"SHOW {k}").fetchone()[0]
            for k in ("log_connections", "log_disconnections", "log_statement")
        }
    check(
        settings == {"log_connections": "on", "log_disconnections": "on", "log_statement": "ddl"},
        "connection, disconnection and DDL logging on",
        str(settings),
    )
    cid = subprocess.run(
        ["docker", "ps", "-q", "--filter", "label=com.docker.compose.service=postgres-analytics"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    logs = (
        subprocess.run(["docker", "logs", "--since", "2m", cid[0]], capture_output=True, text=True)
        if cid
        else None
    )
    text = (logs.stdout + logs.stderr) if logs else ""
    check(
        f"application_name={marker}" in text and "SSL enabled" in text,
        "this proof's TLS connection is in the server log",
        marker,
    )


def encryption_at_rest() -> None:
    print("X7 — encryption at rest")
    s3 = object_store(os.environ.get("MINIO_ROOT_USER"), os.environ.get("MINIO_ROOT_PASSWORD"))
    buckets = [
        os.environ.get("LANDING_INBOX_BUCKET", "landing-inbox"),
        os.environ.get("LANDING_ARCHIVE_BUCKET", "landing-archive"),
        os.environ.get("STATE_BUCKET", "platform-state"),
        os.environ.get("BACKUP_BUCKET", "platform-backups"),
    ]
    for b in buckets:
        rule = s3.get_bucket_encryption(Bucket=b)["ServerSideEncryptionConfiguration"]["Rules"][0]
        algo = rule["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"]
        keys = [
            o["Key"]
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=b)
            for o in page.get("Contents", [])
        ]
        plain = [k for k in keys if not s3.head_object(Bucket=b, Key=k).get("ServerSideEncryption")]
        check(
            algo == "AES256" and not plain,
            f"{b}: SSE by default, every object encrypted",
            f"{len(keys)} objects, {len(plain)} plain",
        )


def gates() -> None:
    print("X2 / X8 / X10 — executable gates")
    pre = subprocess.run(
        [sys.executable, str(REPO / "platform" / "core" / "infra" / "preflight.py")],
        env={**os.environ, "DGP_ENV": "prod"},
        capture_output=True,
        text=True,
    )
    check(
        pre.returncode == 1,
        "X2: production preflight refuses the shipped dev credentials",
        pre.stdout.splitlines()[0][:90] if pre.stdout else "",
    )
    pin = subprocess.run(
        [sys.executable, str(REPO / "governance" / "policies" / "check_pinned_images.py")],
        capture_output=True,
        text=True,
    )
    check(pin.returncode == 0, "X8: every image and action pinned", pin.stdout.strip()[:90])
    dr = subprocess.run(
        [sys.executable, str(REPO / "platform" / "core" / "recovery" / "status.py")],
        capture_output=True,
        text=True,
    )
    check(
        dr.returncode == 0,
        "X10: backups fresh and a drill passed recently",
        dr.stdout.strip().splitlines()[-1] if dr.stdout else dr.stderr[-200:],
    )


def main() -> int:
    tls()
    least_privilege()
    audit_logging()
    encryption_at_rest()
    gates()
    return check.verdict(
        "TLS-only, least privilege, audit logging, encryption at rest, gates all hold."
    )


if __name__ == "__main__":
    sys.exit(main())
