#!/usr/bin/env python3
"""Automated restore drill — proof P9 (docs/disaster-recovery.md §5).

A backup is only trusted once it has been restored. Against the latest backup in
``s3://$BACKUP_BUCKET/backups/``, into a fresh, isolated sandbox (``dgp-drill`` compose project —
own network, own volumes, nothing shared with the live platform):

  0. RPO: commit a heartbeat row on the LIVE shop_oltp (after the backup was taken) and measure how
     long until its WAL segment is archived — the real data-loss window, checked against the RPO;
  1. download every backup file and verify its sha256 against the manifest;
  2. RESTORE the stores that cannot be regenerated and check each holds at least the rows counted at
     backup time. shop_oltp is POINT-IN-TIME recovered — base backup + replay of the WAL archive —
     and must contain the heartbeat committed AFTER the base backup (roll-forward proven); DataHub,
     Airflow, the ingestion ledger and Superset metadata are restored from their dumps;
  3. REBUILD the analytics warehouse instead of restoring it (DR rule: regenerate, don't restore):
     roles/schemas from the git init script, raw loaded from the landing zone + the RESTORED
     shop_oltp through the real ingestion gate, then every domain published with write-audit-publish
     — so every contract quality check runs against the recovered data;
  4. verify the rebuilt output ports match the pre-disaster row counts and that access control
     survived (proof P8 re-run against the sandbox: raw denied, PII masked, compliance unmasks);
  5. measure each store's recovery time against its RTO and write the report to
     ``s3://$BACKUP_BUCKET/drills/<timestamp>.json``.

The landing zone is read in place: it is the tier-1 source of truth, protected by versioning +
object lock + replication rather than by a copy (the drill asserts versioning is on).

Exit 0 only if every check passed and every RTO was met. Run: ``make restore-drill`` (or
``make prove-p9``, which takes a fresh backup first). ``--keep`` leaves the sandbox up to inspect.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from common import (
    BACKUP_BUCKET,
    DRILL_PREFIX,
    REPO_ROOT,
    RPO_SECONDS,
    RTO_SECONDS,
    container,
    latest_manifest,
    s3,
    sha256,
)

DRILL_OLTP_PORT = os.environ.get("DRILL_OLTP_PORT", "55432")
DRILL_ANALYTICS_PORT = os.environ.get("DRILL_ANALYTICS_PORT", "55433")
COMPOSE = [
    "docker", "compose", "--env-file", str(REPO_ROOT / ".env"), "-p", "dgp-drill",
    "-f", str(REPO_ROOT / "platform" / "core" / "recovery" / "docker-compose.drill.yml"),
]  # fmt: skip
PITR_CONF = "restore_command = 'cp /wal-archive/%f %p'\nrecovery_target_timeline = 'latest'"
# Seed the empty data volume with the base backup and arm recovery from the WAL archive.
PITR_SEED = (
    "set -e; cd /var/lib/postgresql/data; tar -x; "
    'echo "$0" >> postgresql.auto.conf; touch recovery.signal; '
    "chown -R postgres:postgres .; chmod 700 ."
)
PG_RESTORE = 'pg_restore -U "$POSTGRES_USER" -d "$0" --no-owner --no-acl --exit-on-error'


class Drill:
    def __init__(self) -> None:
        self.t0 = time.monotonic()
        self.checks: list[tuple[str, bool, str]] = []
        self.recovered_at: dict[str, float] = {}

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append((name, ok, detail))
        print(f"  {'ok  ' if ok else 'FAIL'} {name}{' — ' + detail if detail else ''}")
        return ok

    def recovered(self, store: str) -> None:
        self.recovered_at[store] = time.monotonic() - self.t0

    # ------------------------------------------------------------------ sandbox helpers ---
    def exec(self, svc: str, shell: str, arg: str = "", stdin: Path | None = None) -> str:
        cmd = [*COMPOSE, "exec", "-T", svc, "sh", "-c", shell, arg]
        if stdin is None:
            return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()
        # Pass bytes, not the file: subprocess reads a GzipFile through its raw (compressed) fd.
        opener = gzip.open if stdin.suffix == ".gz" else open
        with opener(stdin, "rb") as f:
            proc = subprocess.run(cmd, input=f.read(), capture_output=True)
        if proc.returncode != 0:
            err = proc.stderr.decode(errors="replace")[-2000:]
            raise RuntimeError(f"{svc}: {shell} failed:\n{err}")
        return proc.stdout.decode().strip()

    def oneoff(self, svc: str, shell: str, arg: str, stdin: Path) -> None:
        """Run ``shell`` in a throwaway container of ``svc`` (its volumes) with stdin."""
        cmd = [
            *COMPOSE,
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--entrypoint",
            "sh",
            svc,
            "-c",
            shell,
            arg,
        ]
        with gzip.open(stdin, "rb") as f:
            proc = subprocess.run(cmd, input=f.read(), capture_output=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"{svc}: seeding failed:\n{proc.stderr.decode(errors='replace')[-2000:]}"
            )

    def pg_count(self, svc: str, db: str, table: str) -> int:
        return int(
            self.exec(
                svc,
                'psql -U "$POSTGRES_USER" -d "$0" -tAc "SELECT count(*) FROM ' + table + '"',
                db,
            )
        )

    def at_least(self, store: str, actual: dict[str, int], expected: dict[str, int]) -> bool:
        short = {t: (actual.get(t, 0), n) for t, n in expected.items() if actual.get(t, 0) < n}
        return self.check(
            f"{store}: restored rows ≥ backup-time counts",
            not short,
            f"{len(expected)} tables" if not short else f"short: {short}",
        )


def _drill_env() -> dict[str, str]:
    """The platform's own env, pointed at the sandbox databases (MinIO/landing stays live)."""
    env = dict(os.environ)
    for k in ("OLTP_INTERNAL_PORT", "ANALYTICS_INTERNAL_PORT"):
        env.pop(k, None)
    env.update(
        OLTP_HOST="localhost", OLTP_PORT=DRILL_OLTP_PORT,
        ANALYTICS_HOST="localhost", ANALYTICS_PORT=DRILL_ANALYTICS_PORT,
    )  # fmt: skip
    return env


def _heartbeat() -> tuple[int, float]:
    """Commit a heartbeat on the live shop_oltp; return (id, seconds until its WAL was archived)."""
    cid = container("governance-platform", "postgres-oltp")
    if not cid:
        raise SystemExit("live postgres-oltp is not running — the RPO check needs it")

    def psql(*sql: str) -> list[str]:
        args = [a for q in sql for a in ("-c", q)]
        out = subprocess.run(
            ["docker", "exec", cid, "psql", "-U", os.environ.get("OLTP_SUPERUSER", "postgres"),
             "-d", os.environ.get("OLTP_DB", "shop_oltp"), "-tAq", *args],
            capture_output=True, text=True, check=True,
        ).stdout  # fmt: skip
        return [line for line in out.splitlines() if line.strip()]

    hb_id, segment = psql(
        "INSERT INTO platform_dr.heartbeat (note) VALUES ('restore drill') RETURNING id",
        "SELECT pg_walfile_name(pg_current_wal_lsn())",
    )
    start = time.monotonic()
    while time.monotonic() - start < RPO_SECONDS["shop_oltp"]:
        last = (psql("SELECT coalesce(last_archived_wal, '') FROM pg_stat_archiver") or [""])[0]
        if last[:24] >= segment:
            return int(hb_id), time.monotonic() - start
        time.sleep(2)
    return int(hb_id), float("inf")


def _latest_partition(client) -> str:
    """Newest ``dt=YYYY-MM-DD`` partition of accepted landing files — the date the rebuild replays
    as of (each dataset then loads its newest accepted file on or before it).

    Only ``accepted/`` counts: those files passed the contract gate and are object-locked
    evidence (ADR-0024); quarantine and the inbox are never a rebuild source.
    """
    bucket = os.environ.get("LANDING_ARCHIVE_BUCKET", "landing-archive")
    dates: set[str] = set()
    pages = client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="accepted/")
    for page in pages:
        for o in page.get("Contents", []):
            if m := re.search(r"/dt=(\d{4}-\d{2}-\d{2})", o["Key"]):
                dates.add(m.group(1))
    # A partition dated in the future (a partner's wrong business date) must not define "latest".
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    dates = {d for d in dates if d <= today}
    if not dates:
        raise SystemExit(f"{bucket}/accepted/ has no partitions — nothing to rebuild from")
    return max(dates)


def run(keep: bool) -> int:
    d = Drill()
    client = s3()
    prefix, manifest = latest_manifest(client)
    hb_id, archived_in = _heartbeat()
    d.check(
        "shop_oltp: a committed write reached the WAL archive within the RPO",
        archived_in <= RPO_SECONDS["shop_oltp"],
        f"heartbeat #{hb_id} archived in {archived_in:.0f}s (RPO {RPO_SECONDS['shop_oltp']}s)",
    )
    d.t0 = time.monotonic()  # recovery time is measured from here, not from the RPO wait
    arts = manifest["artifacts"]
    print(f"Restore drill — backup s3://{BACKUP_BUCKET}/{prefix} (taken {manifest['created_at']})")
    for name, why in manifest.get("skipped", {}).items():
        print(f"  note {name} was not in this backup ({why})")

    with tempfile.TemporaryDirectory() as tmp:
        # 1. fetch + integrity -------------------------------------------------------------
        files: dict[str, Path] = {}
        for name, a in arts.items():
            dest = Path(tmp) / Path(a["key"]).name
            client.download_file(BACKUP_BUCKET, a["key"], str(dest))
            d.check(f"{name}: sha256 matches manifest", sha256(dest) == a["sha256"])
            files[name] = dest
        archive = os.environ.get("LANDING_ARCHIVE_BUCKET", "landing-archive")
        lock = client.get_object_lock_configuration(Bucket=archive)["ObjectLockConfiguration"]
        d.check(
            "landing archive: object lock enabled (tier-1 protection, ADR-0024)",
            lock.get("ObjectLockEnabled") == "Enabled",
            str(lock.get("ObjectLockEnabled")),
        )

        # 2. fresh isolated sandbox ---------------------------------------------------------
        subprocess.run([*COMPOSE, "down", "-v", "--remove-orphans"], capture_output=True)
        # drill-oltp starts only after its data volume is seeded with the base backup (below).
        others = ["drill-analytics", "drill-meta", "drill-datahub-db"]
        subprocess.run([*COMPOSE, "up", "-d", "--wait", *others], check=True, capture_output=True)
        print(f"  sandbox up ({time.monotonic() - d.t0:.0f}s)")

        try:
            # 3. restore what can't be regenerated -----------------------------------------
            if "shop_oltp" in files:
                db = os.environ.get("OLTP_DB", "shop_oltp")
                # Point-in-time recovery: base backup into the empty data dir, then Postgres replays
                # the WAL archive to its end and promotes (whole cluster: data, roles, grants).
                d.oneoff("drill-oltp", PITR_SEED, PITR_CONF, stdin=files["shop_oltp"])
                subprocess.run(
                    [*COMPOSE, "up", "-d", "--wait", "drill-oltp"], check=True, capture_output=True
                )
                counts = {t: d.pg_count("drill-oltp", db, t) for t in arts["shop_oltp"]["counts"]}
                rolled = d.exec(
                    "drill-oltp",
                    f'psql -U "$POSTGRES_USER" -d "$0" -tAc '
                    f'"SELECT count(*) FROM platform_dr.heartbeat WHERE id = {hb_id}"',
                    db,
                ) == "1"  # fmt: skip
                d.check(
                    "shop_oltp: PITR replayed WAL past the base backup (post-backup row back)",
                    rolled,
                    f"heartbeat #{hb_id}",
                )
                if d.at_least("shop_oltp", counts, arts["shop_oltp"]["counts"]) and rolled:
                    d.recovered("shop_oltp")

            for name in ("airflow", "ingestion_ledger"):
                if name in files:
                    d.exec("drill-meta", 'createdb -U "$POSTGRES_USER" "$0"', name)
                    d.exec("drill-meta", PG_RESTORE, name, stdin=files[name])
                    counts = {t: d.pg_count("drill-meta", name, t) for t in arts[name]["counts"]}
                    if d.at_least(name, counts, arts[name]["counts"]):
                        d.recovered(name)

            if "datahub" in files:
                d.exec(
                    "drill-datahub-db",
                    'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" datahub',
                    stdin=files["datahub"],
                )
                n = int(d.exec(
                    "drill-datahub-db",
                    'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -N -e '
                    '"SELECT count(*) FROM metadata_aspect_v2" datahub 2>/dev/null',
                ))  # fmt: skip
                if d.at_least("datahub", {"metadata_aspect_v2": n}, arts["datahub"]["counts"]):
                    d.recovered("datahub")

            if "superset" in files:
                con = sqlite3.connect(files["superset"])
                ok = con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                d.check("superset: SQLite integrity_check", ok)
                counts = {
                    t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                    for t in arts["superset"]["counts"]
                }  # noqa: S608
                con.close()
                if ok and d.at_least("superset", counts, arts["superset"]["counts"]):
                    d.recovered("superset")

            # 4. rebuild analytics from git + landing + restored OLTP -------------------------
            env = _drill_env()
            partition = _latest_partition(client)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            print(
                f"  rebuilding analytics: replay (archive accepted/ dt={partition} + restored "
                "shop_oltp) → publish"
            )
            load = subprocess.run(
                ["uv", "run", "dgp-ingest", "replay",
                 "--partition-date", partition, "--run-id", f"drill-{stamp}"],
                cwd=REPO_ROOT, env=env, capture_output=True, text=True,
            )  # fmt: skip
            d.check(
                "analytics: raw replayed from accepted (gated) landing files",
                load.returncode == 0,
                load.stderr[-400:] if load.returncode else "",
            )
            pub = subprocess.run(
                # --no-state: the drill's rebuild must not replace the live dbt state baseline.
                ["uv", "run", "--group", "dbt", "python", "platform/core/transform/publish.py",
                 "--no-state"],
                cwd=REPO_ROOT, env=env, capture_output=True, text=True,
            )  # fmt: skip
            passed = re.findall(r"Done\. PASS=(\d+) WARN=(\d+) ERROR=(\d+)", pub.stdout)
            summary = ", ".join(f"PASS={p} WARN={w} ERROR={e}" for p, w, e in passed)
            d.check(
                "analytics: every domain published — all contract + engineering DQ passed",
                pub.returncode == 0,
                summary,
            )

            ports = manifest.get("ports", {})
            got = {
                p: d.pg_count("drill-analytics", os.environ.get("ANALYTICS_DB", "analytics"), p)
                for p in ports
            }
            if d.check(
                "analytics: rebuilt output ports == pre-disaster row counts", got == ports, str(got)
            ):
                d.recovered("analytics")

            p8 = subprocess.run(
                ["uv", "run", "--no-project", "--with", "psycopg[binary]",
                 "python", "proofs/p8_access_control.py"],
                cwd=REPO_ROOT, env=env, capture_output=True, text=True,
            )  # fmt: skip
            d.check(
                "access control survived the rebuild (P8 against the sandbox)",
                p8.returncode == 0,
                p8.stdout.strip().splitlines()[0] if p8.stdout else p8.stderr[-300:],
            )
        finally:
            if keep:
                print(
                    f"  --keep: sandbox left up (oltp :{DRILL_OLTP_PORT}, "
                    f"analytics :{DRILL_ANALYTICS_PORT})"
                )
            else:
                subprocess.run([*COMPOSE, "down", "-v", "--remove-orphans"], capture_output=True)

    # 5. RTO report ---------------------------------------------------------------------------
    total = time.monotonic() - d.t0
    expected = set(arts) | {"analytics"}
    rto = []
    print("\n  store              recovered in     RTO      met")
    for store in sorted(expected, key=lambda s: RTO_SECONDS[s]):
        took = d.recovered_at.get(store)
        met = took is not None and took <= RTO_SECONDS[store]
        rto.append({"store": store, "seconds": took, "rto_seconds": RTO_SECONDS[store], "met": met})
        shown = f"{took:8.0f}s" if took is not None else "   failed"
        print(
            f"  {store:<18} {shown}     {RTO_SECONDS[store] / 3600:4.0f} h    "
            f"{'yes' if met else 'NO'}"
        )

    passed = all(ok for _, ok, _ in d.checks) and all(r["met"] for r in rto)
    report = {
        "drill_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "backup": prefix, "passed": passed, "total_seconds": round(total, 1),
        "checks": [{"check": c, "ok": ok, "detail": det} for c, ok, det in d.checks],
        "rto": rto, "skipped_in_backup": manifest.get("skipped", {}),
    }  # fmt: skip
    key = f"{DRILL_PREFIX}{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    client.put_object(
        Bucket=BACKUP_BUCKET,
        Key=key,
        Body=json.dumps(report, indent=2).encode(),
        ContentType="application/json",
    )
    print(
        f"\nP9 {'OK' if passed else 'FAILED'} — drill took {total:.0f}s; report s3://{BACKUP_BUCKET}/{key}"
    )
    return 0 if passed else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Restore the latest backup into a sandbox and prove it."
    )
    parser.add_argument("--keep", action="store_true", help="leave the drill sandbox running")
    return run(parser.parse_args().keep)


if __name__ == "__main__":
    sys.exit(main())
