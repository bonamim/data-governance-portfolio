#!/usr/bin/env python3
"""DR freshness check — the alert conditions for backups and drills (production-readiness X10).

Fails (exit 1) when:
  * the newest backup is older than ``BACKUP_MAX_AGE_HOURS`` (default 26: daily + slack), or
  * there is no PASSING restore drill newer than ``DRILL_MAX_AGE_DAYS`` (default 7: weekly drills).

A monitor runs this on a schedule and pages on failure — "backups silently stopped" and "nobody has
proven a restore lately" are the two DR failures that otherwise go unnoticed.
Run: ``make dr-status``.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta

from common import BACKUP_BUCKET, BACKUP_PREFIX, DRILL_PREFIX, s3

BACKUP_MAX_AGE = timedelta(hours=float(os.environ.get("BACKUP_MAX_AGE_HOURS", "26")))
DRILL_MAX_AGE = timedelta(days=float(os.environ.get("DRILL_MAX_AGE_DAYS", "7")))


def _keys(client, prefix: str, suffix: str) -> list[str]:
    keys: list[str] = []
    for page in client.get_paginator("list_objects_v2").paginate(
        Bucket=BACKUP_BUCKET, Prefix=prefix
    ):
        keys += [o["Key"] for o in page.get("Contents", []) if o["Key"].endswith(suffix)]
    return sorted(keys)


def _stamp(key: str) -> datetime:
    """``backups/<stamp>/manifest.json`` or ``drills/<stamp>.json`` -> the stamp's datetime."""
    token = next(p for p in key.replace(".json", "").split("/") if p[:8].isdigit())
    return datetime.strptime(token, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)


def main() -> int:
    client = s3()
    now = datetime.now(UTC)
    problems: list[str] = []

    backups = _keys(client, BACKUP_PREFIX, "manifest.json")
    if not backups:
        problems.append("no backup exists")
    else:
        age = now - _stamp(backups[-1])
        status = "ok" if age <= BACKUP_MAX_AGE else "STALE"
        print(
            f"  latest backup  {backups[-1].split('/')[1]}  age {age}  "
            f"(max {BACKUP_MAX_AGE})  {status}"
        )
        if age > BACKUP_MAX_AGE:
            problems.append(f"latest backup is {age} old (max {BACKUP_MAX_AGE})")

    passing = None
    for key in reversed(_keys(client, DRILL_PREFIX, ".json")):
        report = json.loads(client.get_object(Bucket=BACKUP_BUCKET, Key=key)["Body"].read())
        if report.get("passed"):
            passing = key
            break
    if not passing:
        problems.append("no passing restore drill exists")
    else:
        age = now - _stamp(passing)
        status = "ok" if age <= DRILL_MAX_AGE else "STALE"
        print(
            f"  latest passing drill  {passing.split('/')[1]}  age {age}  "
            f"(max {DRILL_MAX_AGE})  {status}"
        )
        if age > DRILL_MAX_AGE:
            problems.append(f"latest passing drill is {age} old (max {DRILL_MAX_AGE})")

    if problems:
        print("DR status FAILED — alert:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("DR status OK: backups fresh, restore proven recently.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
