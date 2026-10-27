#!/usr/bin/env python3
"""Proof P8: access control. Consumers see PII masked and are denied raw/staging (ADR-0013, M8).

The masking policy is declared on the column in ODCS and enforced as a role-aware Postgres view
(dgp_platform_access.apply_masking). This proves the three guarantees, against the live warehouse:

  1. a least-privilege consumer (``analytics_reader``) is **denied** the raw schema and the base
     output-port table, and can only reach the masking view.
  2. through that view the consumer sees the PII **masked**: the value equals ``md5(raw)``, not the
     real identifier.
  3. the ``compliance`` unmask role sees the **raw** value through the same view.

Deterministic (no LLM). Needs the live platform with the port built + masked (``make transform`` or
``make mask``). Run: ``make prove-p8``.
"""

from __future__ import annotations

import hashlib
import os
import sys

import psycopg
from psycopg import errors

HOST = os.environ.get("ANALYTICS_HOST", "localhost")
PORT = os.environ.get("ANALYTICS_PORT", "5433")
DB = os.environ.get("ANALYTICS_DB", "analytics")
VIEW = "customer_masked.customers"
BASE = "customer_silver.customers"
RAW = "customer_raw.customers"


def _conn(role: str, pw_env: str) -> psycopg.Connection:
    pw = os.environ[pw_env]
    return psycopg.connect(f"host={HOST} port={PORT} dbname={DB} user={role} password={pw}")


def _denied(conn: psycopg.Connection, relation: str) -> bool:
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM {relation} LIMIT 1")
            cur.fetchone()
        return False
    except errors.InsufficientPrivilege:
        conn.rollback()
        return True


def main() -> int:
    failures: list[str] = []

    # compliance (unmask): read the raw value for a known row through the view.
    with _conn("compliance", "COMPLIANCE_PASSWORD") as c:
        row = c.execute(
            f"SELECT customer_id, customer_unique_id FROM {VIEW} ORDER BY customer_id LIMIT 1"
        ).fetchone()
    if not row:
        print("P8: no rows in the port. Run `make transform` first.")
        return 2
    customer_id, raw_value = row

    # analytics_reader (consumer): denied raw/base, and sees the masked value through the view.
    with _conn("analytics_reader", "ANALYTICS_READER_PASSWORD") as r:
        if not _denied(r, RAW):
            failures.append(f"analytics_reader could read {RAW} (raw schema must be denied)")
        if not _denied(r, BASE):
            failures.append(f"analytics_reader could read {BASE} (base port table must be denied)")
        masked = r.execute(
            f"SELECT customer_unique_id FROM {VIEW} WHERE customer_id = %s", (customer_id,)
        ).fetchone()[0]

    expected_mask = hashlib.md5(raw_value.encode()).hexdigest()  # noqa: S324 (demo mask, not security)
    if masked == raw_value:
        failures.append("analytics_reader saw the RAW value through the view (not masked)")
    elif masked != expected_mask:
        failures.append(f"masked value {masked!r} != md5(raw) {expected_mask!r}")

    if failures:
        print("P8 FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("P8 OK:")
    print(f"  - analytics_reader: denied {RAW} and {BASE} (raw/staging protected)")
    print(f"  - analytics_reader via {VIEW}: sees masked md5 ({masked[:12]}…), not the raw id")
    print(f"  - compliance via {VIEW}: sees the raw id ({raw_value[:12]}…)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
