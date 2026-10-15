#!/usr/bin/env python3
"""Back up every store that cannot be regenerated (docs/disaster-recovery.md §2–3, proof P9).

The DR design rule: anything regenerable from git or the sources is REBUILT, not restored — so the
``analytics`` warehouse is not dumped (the drill rebuilds it), only its ingestion ledger is. What is
backed up, each into ``s3://$BACKUP_BUCKET/backups/<UTC timestamp>/``:

  shop_oltp         PHYSICAL base backup (pg_basebackup, tar) of the operational source (tier 1);
                    with the continuous WAL archive it gives point-in-time recovery (RPO 5 min)
  ingestion_ledger  pg_dump -Fc of ``platform_meta`` in analytics (tier 2 — pipeline state)
  airflow           pg_dump -Fc of the Airflow metadata DB (tier 2 — run history, data intervals)
  datahub           mysqldump of the DataHub metadata store (tier 2 — manual edits not in git)
  superset          online SQLite backup of the Superset metadata DB (tier 3; dashboards in git)

plus ``manifest.json``: per file its sha256 and the row counts of key tables taken just BEFORE the
dump (so a restore must hold at least those rows), and the row count of every output port — the
baseline the drill's rebuilt warehouse must reproduce. A store whose service is not running is
recorded as skipped, never silently dropped.

After a successful base backup the WAL archive is pruned to what that base needs
(``pg_archivecleanup``), so the latest base + archive always replays to the present.

Local stand-in: ``pg_basebackup`` + an archive volume, logical dumps for the metadata stores.
Production uses pgBackRest (full + diff + continuous WAL archiving,
``repo1-cipher-type=aes-256-cbc``) into an object-locked, replicated bucket; the drill and the
manifest contract stay the same.

Run: ``make backup``.
"""

from __future__ import annotations

import gzip
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from common import BACKUP_BUCKET, BACKUP_PREFIX, container, output_ports, s3, sha256

PLATFORM, CATALOG, BI = "governance-platform", "dgp-catalog", "dgp-bi"

PG_SH = 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "$0"'
MYSQL_SH = 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -N -e "$0" datahub 2>/dev/null'
SUPERSET_BACKUP = (
    "import sqlite3; s=sqlite3.connect('/app/superset_home/superset.db'); "
    "d=sqlite3.connect('/tmp/superset_backup.db'); s.backup(d); d.close()"
)


def _exec(cid: str, shell: str, arg: str = "") -> str:
    return subprocess.run(
        ["docker", "exec", cid, "sh", "-c", shell, arg], capture_output=True, text=True, check=True
    ).stdout.strip()


def _dump(cid: str, shell: str, dest: Path, compress: bool = False) -> None:
    """Stream ``docker exec <shell>`` stdout into ``dest`` (gzip-compressed if asked)."""
    proc = subprocess.Popen(["docker", "exec", cid, "sh", "-c", shell], stdout=subprocess.PIPE)
    opener = gzip.open if compress else open
    with opener(dest, "wb") as out:
        shutil.copyfileobj(proc.stdout, out)
    if proc.wait() != 0:
        raise RuntimeError(f"dump failed in {cid}: {shell}")


def _prune_wal_archive(cid: str) -> None:
    """Drop archived WAL older than the newest base backup (its ``.backup`` label marks it)."""
    shell = (
        "label=$(ls /wal-archive/*.backup 2>/dev/null | sort | tail -1) && "
        '[ -n "$label" ] && pg_archivecleanup /wal-archive "$(basename "$label")"'
    )
    subprocess.run(["docker", "exec", cid, "sh", "-c", shell], check=False)


def _pg_counts(cid: str, tables: list[str]) -> dict[str, int]:
    return {t: int(_exec(cid, PG_SH, f"SELECT count(*) FROM {t}")) for t in tables}


def _stores() -> list[dict]:
    """What to back up: name, tier, (project, service), dump command, counted tables."""
    return [
        {
            "name": "shop_oltp", "tier": 1, "svc": (PLATFORM, "postgres-oltp"),
            "file": "shop_oltp.base.tar.gz", "compress": True,
            # Physical base backup to stdout (tar); WAL needed for consistency fetched into it.
            "dump": 'pg_basebackup -U "$POSTGRES_USER" -D - -Ft -X fetch -c fast',
            "count_sql": "SELECT string_agg(schemaname||'.'||relname, ',') "
            "FROM pg_stat_user_tables "
            "WHERE schemaname = 'shop'",
        },
        {
            "name": "ingestion_ledger", "tier": 2, "svc": (PLATFORM, "postgres-analytics"),
            "file": "ingestion_ledger.dump",
            "dump": 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -n platform_meta -Fc',
            "tables": ["platform_meta.ingestion_ledger"],
        },
        {
            "name": "airflow", "tier": 2, "svc": (PLATFORM, "postgres-airflow"),
            "file": "airflow.dump",
            "dump": 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc',
            "tables": ["dag", "dag_run", "task_instance"],
        },
        {
            "name": "datahub", "tier": 2, "svc": (CATALOG, "mysql"), "file": "datahub.sql.gz",
            "dump": 'mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --single-transaction --routines '
            "--triggers --no-tablespaces datahub 2>/dev/null",
            "compress": True,
        },
        {
            "name": "superset", "tier": 3, "svc": (BI, "superset"), "file": "superset.db",
            "dump": f'python -c "{SUPERSET_BACKUP}" && cat /tmp/superset_backup.db '
            "&& rm -f /tmp/superset_backup.db",
        },
    ]  # fmt: skip


def _counts(store: dict, cid: str) -> dict[str, int]:
    if store["name"] == "datahub":
        return {
            "metadata_aspect_v2": int(
                _exec(cid, MYSQL_SH, "SELECT count(*) FROM metadata_aspect_v2")
            )
        }
    if store["name"] == "superset":
        q = ", ".join(f"(SELECT count(*) FROM {t})" for t in ("dashboards", "slices", "tables"))
        db = "sqlite3.connect('/app/superset_home/superset.db')"
        py = f"import sqlite3; print(*{db}.execute('SELECT {q}').fetchone())"
        d, s, t = _exec(cid, f'python -c "{py}"').split()
        return {"dashboards": int(d), "slices": int(s), "tables": int(t)}
    tables = store.get("tables") or _exec(cid, PG_SH, store["count_sql"]).split(",")
    return _pg_counts(cid, tables)


def main() -> int:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    prefix = f"{BACKUP_PREFIX}{stamp}/"
    client = s3()
    try:
        client.head_bucket(Bucket=BACKUP_BUCKET)
    except client.exceptions.ClientError:
        print(f"bucket {BACKUP_BUCKET} missing — run `make up` (minio-init creates it)")
        return 2

    manifest: dict = {"created_at": stamp, "bucket": BACKUP_BUCKET, "artifacts": {}, "skipped": {}}
    with tempfile.TemporaryDirectory() as tmp:
        for store in _stores():
            cid = container(*store["svc"])
            if not cid:
                manifest["skipped"][store["name"]] = f"{'/'.join(store['svc'])} not running"
                print(f"  skip {store['name']:<17} ({manifest['skipped'][store['name']]})")
                continue
            counts = _counts(store, cid)  # BEFORE the dump: the restore must hold at least these
            dest = Path(tmp) / store["file"]
            _dump(cid, store["dump"], dest, compress=store.get("compress", False))
            key = prefix + store["file"]
            client.upload_file(str(dest), BACKUP_BUCKET, key)
            manifest["artifacts"][store["name"]] = {
                "key": key, "tier": store["tier"], "sha256": sha256(dest),
                "bytes": dest.stat().st_size, "counts": counts,
            }  # fmt: skip
            print(f"  ok   {store['name']:<17} {dest.stat().st_size / 1e6:7.1f} MB  {counts}")
            if store["name"] == "shop_oltp":
                _prune_wal_archive(cid)

    # Baseline for the rebuild: row count of every output port in the live warehouse.
    ana = container(PLATFORM, "postgres-analytics")
    if ana:
        manifest["ports"] = _pg_counts(ana, output_ports())
        print(f"  ports baseline    {manifest['ports']}")

    client.put_object(
        Bucket=BACKUP_BUCKET, Key=prefix + "manifest.json",
        Body=json.dumps(manifest, indent=2).encode(), ContentType="application/json",
    )  # fmt: skip
    print(f"backup written: s3://{BACKUP_BUCKET}/{prefix}")
    if "shop_oltp" not in manifest["artifacts"]:
        print("FAILED: the tier-1 store shop_oltp was not backed up")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
