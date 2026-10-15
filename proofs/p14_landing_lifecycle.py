#!/usr/bin/env python3
"""Proof P14 — landing-zone lifecycle, end to end on the live platform (ADR-0024).

A partner delivers files into the inbox; the platform files each one to ``accepted/`` (locked,
loaded) or ``quarantine/`` (with ``_reason.json``, not loaded); the results appear in DataHub as
assertions on the landing dataset. Exercised with the ``reviews`` dataset:

  0. layout     archive has object lock + one lifecycle rule per retention class + quarantine rule
  1. bad data   null order_id + review_score 99 -> quarantined, reason recorded, raw untouched
  2. bad name   a file not following the partner's naming convention -> quarantined (naming)
  3. good       a conforming file -> accepted/ (partition from the FILE NAME, lock, retention
                tag), loaded, inbox drained
  4. real       the real Olist file -> accepted + loaded (restores the real data)
  5. resend     the same file again -> skipped (idempotent, P6), inbox drained
  6. access     partner is write-only to its own prefix; nobody deletes accepted evidence
  7. catalog    `make catalog-quality` -> DataHub shows FAILURE/SUCCESS runs per partition

Leaves customer_experience_raw.reviews holding the real Olist file, as before. Run:
``make prove-p14`` (``--skip-catalog`` to skip step 7 when DataHub is not running).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
import psycopg
from botocore.exceptions import ClientError
from dgp_platform_ingestion import db, layout, pipeline
from dgp_platform_ingestion.objectstore import ObjectStore
from dgp_platform_ingestion.settings import Settings

REPO = Path(__file__).resolve().parents[1]
DOMAIN, SOURCE, DATASET = "customer_experience", "olist", "reviews"
REAL = REPO / "data" / "olist" / "olist_order_reviews_dataset.csv"
GQL = f"http://localhost:{os.environ.get('DATAHUB_MAPPED_GMS_PORT', '8081')}/api/graphql"
HEADER = [
    "review_id", "order_id", "review_score", "review_comment_title",
    "review_comment_message", "review_creation_date", "review_answer_timestamp",
]  # fmt: skip

failures: list[str] = []


def check(ok: bool, what: str, detail: str = "") -> bool:
    print(f"  {'OK  ' if ok else 'FAIL'} {what}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(what)
    return ok


def _csv(rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(HEADER)
    w.writerows(rows)
    return buf.getvalue().encode()


def _rows(n: int = 1001) -> list[list[str]]:
    return [[f"p14r{i}", f"p14o{i}", "4", "", "ok", "2026-01-01", ""] for i in range(n)]


def _denied(fn) -> bool:
    try:
        fn()
    except ClientError as e:
        return e.response["Error"]["Code"] in ("AccessDenied", "403", "Forbidden")
    return False


class Ctx:
    def __init__(self) -> None:
        self.settings = Settings.from_env()
        self.loader = ObjectStore(self.settings.objectstore)
        access, secret = self.settings.objectstore.partner_credentials(SOURCE)
        if not secret:
            sys.exit("LANDING_PARTNER_OLIST_SECRET is not set — run `make up` with the new .env")
        self.partner = ObjectStore(self.settings.objectstore, access, secret).client
        self.root = boto3.client(
            "s3",
            endpoint_url=os.environ.get("MINIO_ENDPOINT", "http://localhost:9000") or None,
            aws_access_key_id=os.environ.get("MINIO_ROOT_USER"),
            aws_secret_access_key=os.environ.get("MINIO_ROOT_PASSWORD"),
            region_name="us-east-1",
        )
        self.inbox, self.archive = self.loader.inbox, self.loader.archive
        self.stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self.today = datetime.now(UTC)

    def deliver(self, name: str, body: bytes) -> str:
        key = layout.inbox_prefix(SOURCE, DATASET) + name
        self.partner.put_object(Bucket=self.inbox, Key=key, Body=body)
        return key

    def run(self, tag: str) -> tuple[str, dict]:
        run_id = f"p14-{tag}-{self.stamp}"
        out = pipeline.load_raw(REPO, self.settings, REPO / "data", run_id, DOMAIN)
        return run_id, out

    def sql(self, query: str, *args):
        with db.connect(self.settings.analytics_loader) as conn:
            return conn.execute(query, args).fetchall()

    def raw_count(self) -> int:
        return self.sql(f"SELECT count(*) FROM {DOMAIN}_raw.{DATASET}")[0][0]

    def ledger(self, run_id: str) -> list[tuple]:
        return self.sql(
            "SELECT id, status, partition, archive_key, checks FROM platform_meta.ingestion_ledger"
            " WHERE run_id = %s AND dataset = %s ORDER BY id",
            run_id,
            f"{SOURCE}/{DATASET}",
        )

    def inbox_empty(self) -> bool:
        return not self.loader.list(self.inbox, layout.inbox_prefix(SOURCE, DATASET))

    def reason(self, archive_key: str) -> dict:
        key = archive_key.rsplit("/", 1)[0] + "/" + layout.REASON_FILE
        return json.load(self.root.get_object(Bucket=self.archive, Key=key)["Body"])


def step_layout(c: Ctx) -> None:
    print("0. Layout: two buckets, lifecycle by stage, object lock on the archive")
    lock = c.root.get_object_lock_configuration(Bucket=c.archive)["ObjectLockConfiguration"]
    check(lock.get("ObjectLockEnabled") == "Enabled", "archive bucket has object lock enabled")
    check("Rule" not in lock, "archive has NO default retention (quarantine must stay deletable)")
    rules = c.root.get_bucket_lifecycle_configuration(Bucket=c.archive)["Rules"]
    tags = sorted(r["Filter"]["Tag"]["Value"] for r in rules if "Tag" in r.get("Filter", {}))
    check(
        tags == sorted(f"{d}d" for d in layout.RETENTION_CLASSES),
        "one archive lifecycle rule per retention class",
        ", ".join(tags),
    )
    q = [r for r in rules if r.get("Filter", {}).get("Prefix") == "quarantine/"]
    check(bool(q) and q[0]["Expiration"]["Days"] == 90, "quarantine/ expires after 90 days")
    inbox = c.root.get_bucket_lifecycle_configuration(Bucket=c.inbox)["Rules"]
    check(any(r.get("Expiration", {}).get("Days") == 30 for r in inbox), "inbox 30-day safety net")


def step_bad_data(c: Ctx) -> None:
    print("1. Contract violation: the file is quarantined with its reason, never loaded")
    before = c.raw_count()
    rows = _rows()
    rows[0] = ["p14X", "", "99", "", "", "2026-01-01", ""]  # order_id missing, score out of range
    body = _csv(rows)
    name = f"olist_order_reviews_{c.today:%Y%m%d}.csv"
    c.deliver(name, body)
    run_id, out = c.run("bad")
    check(out["quarantined"] == 1 and out["loaded"] == 0, "loader outcome: 1 quarantined", str(out))
    (lid, status, partition, key, checks) = c.ledger(run_id)[-1]
    check(status == "quarantined", "ledger records status=quarantined", f"ledger id {lid}")
    check(
        key == layout.quarantine_key(SOURCE, DATASET, f"dt={c.today:%Y-%m-%d}", run_id, name),
        "filed under quarantine/<source>/<dataset>/<partition>/<run_id>/",
        key,
    )
    reason = c.reason(key)
    failed = {(f["check"], f["column"]) for f in reason["failed"]}
    check(
        {("not_null", "order_id"), ("range", "review_score")} <= failed,
        "_reason.json names the failed rules",
        "; ".join(f"{f['check']}({f['column']}): {f['detail']}" for f in reason["failed"]),
    )
    try:
        current = c.root.head_object(
            Bucket=c.archive, Key=layout.accepted_key(SOURCE, DATASET, partition, name)
        )["ETag"].strip('"')
    except ClientError:
        current = None
    check(current != hashlib.md5(body).hexdigest(), "the bad file is NOT in accepted/")  # noqa: S324
    check(c.inbox_empty(), "inbox drained (a bad file is not re-validated every run)")
    check(c.raw_count() == before, "warehouse raw table untouched", f"{before} rows")


def step_bad_name(c: Ctx) -> None:
    print("2. Partner naming convention broken: quarantined by the naming rule")
    c.deliver("reviews_final_v2.csv", _csv(_rows()))
    run_id, out = c.run("name")
    (lid, status, partition, key, checks) = c.ledger(run_id)[-1]
    check(status == "quarantined", "ledger records status=quarantined", f"ledger id {lid}")
    failed = [x for x in checks if not x["passed"]]
    check(
        [x["check"] for x in failed] == ["naming.filename_pattern"],
        "only the naming rule failed",
        failed[0]["detail"] if failed else "",
    )
    check(
        partition == f"dt={c.today:%Y-%m-%d}",
        "an unparseable name is filed by arrival time, so it can be triaged",
        partition,
    )
    check(c.inbox_empty(), "inbox drained")


def step_good(c: Ctx) -> None:
    print("3. Conforming file: accepted (partition from the file name), locked, loaded")
    business = c.today - timedelta(days=1)
    name = f"olist_order_reviews_{business:%Y%m%d}.csv"
    c.deliver(name, _csv(_rows()))
    run_id, out = c.run("good")
    check(out["loaded"] == 1, "loader outcome: 1 loaded", str(out))
    (lid, status, partition, key, checks) = c.ledger(run_id)[-1]
    expected = layout.accepted_key(SOURCE, DATASET, f"dt={business:%Y-%m-%d}", name)
    check(key == expected, "partition comes from the partner's file name (yesterday)", key)
    ret = c.root.get_object_retention(Bucket=c.archive, Key=key)["Retention"]
    days = (ret["RetainUntilDate"] - datetime.now(UTC)).days
    check(
        ret["Mode"] == c.settings.objectstore.lock_mode and 363 <= days <= 365,
        "accepted file is object-locked for the contract's retentionDays",
        f"{ret['Mode']} until {ret['RetainUntilDate']:%Y-%m-%d}",
    )
    tags = c.root.get_object_tagging(Bucket=c.archive, Key=key)["TagSet"]
    check(
        {"Key": "retention-class", "Value": "365d"} in tags,
        "tagged retention-class=365d (picked up by the lifecycle rule)",
    )
    check(c.raw_count() == 1001, "loaded into the warehouse raw table", "1001 rows")
    check(c.inbox_empty(), "inbox drained")
    c.accepted_key = key


def step_real_and_resend(c: Ctx) -> None:
    print("4. Real Olist file: accepted and loaded (restores the real data)")
    if not REAL.exists():
        check(False, f"{REAL} present", "run `make acquire` first")
        return
    body = REAL.read_bytes()
    name = f"olist_order_reviews_{c.today:%Y%m%d}.csv"
    c.deliver(name, body)
    _, out = c.run("real")
    real_rows = sum(1 for _ in csv.reader(io.StringIO(body.decode()))) - 1
    check(out["loaded"] == 1, "loader outcome: 1 loaded", str(out))
    rows = c.raw_count()
    check(
        rows > 99000, "raw table holds the real reviews again", f"{rows} rows ({real_rows} lines)"
    )

    print("5. Re-delivery of the same file: skipped (idempotent, P6)")
    c.deliver(name, body)
    run_id, out = c.run("resend")
    (lid, status, *_rest) = c.ledger(run_id)[-1]
    check(out["skipped"] == 1 and status == "skipped", "ledger records status=skipped", str(out))
    check(c.raw_count() == rows, "warehouse untouched", f"{rows} rows")
    check(c.inbox_empty(), "inbox drained")


def step_access(c: Ctx) -> None:
    print("6. Access: partner write-only to its own prefix; accepted evidence is undeletable")
    p, own = c.partner, layout.inbox_prefix(SOURCE, DATASET)
    check(_denied(lambda: p.list_objects_v2(Bucket=c.inbox, Prefix=own)), "partner cannot list")
    key = c.deliver("p14-probe.txt", b"probe")
    check(_denied(lambda: p.get_object(Bucket=c.inbox, Key=key)), "partner cannot read back")
    check(_denied(lambda: p.delete_object(Bucket=c.inbox, Key=key)), "partner cannot delete")
    check(
        _denied(lambda: p.put_object(Bucket=c.inbox, Key="other_source/x/f.csv", Body=b"x")),
        "partner cannot write another source's prefix",
    )
    check(
        _denied(lambda: p.put_object(Bucket=c.archive, Key="accepted/olist/x.csv", Body=b"x")),
        "partner cannot write the archive",
    )
    c.loader.client.delete_object(Bucket=c.inbox, Key=key)  # tidy the probe (loader may)
    ref = c.loader.head(c.archive, c.accepted_key)
    check(
        _denied(
            lambda: c.loader.client.delete_object(
                Bucket=c.archive, Key=ref.key, VersionId=ref.version_id
            )
        ),
        "loader cannot delete accepted evidence (no Delete* in its policy)",
    )
    # Object lock refuses even the root (MinIO answers InvalidRequest "WORM protected", S3
    # AccessDenied), so the check is on the outcome: the locked version must still exist.
    try:
        c.root.delete_object(Bucket=c.archive, Key=ref.key, VersionId=ref.version_id)
        refused = ""
    except ClientError as e:
        refused = e.response["Error"].get("Message") or e.response["Error"]["Code"]
    survived = c.root.head_object(Bucket=c.archive, Key=ref.key, VersionId=ref.version_id)
    check(
        bool(refused) and survived["VersionId"] == ref.version_id,
        "even the object-store root cannot delete it (object lock)",
        refused,
    )


def _assertion_runs() -> list[dict]:
    urn = f"urn:li:dataset:(urn:li:dataPlatform:s3,{layout.catalog_name(os.environ.get('LANDING_ARCHIVE_BUCKET', 'landing-archive'), SOURCE, DATASET)},PROD)"  # noqa: E501
    q = """query($urn:String!){ dataset(urn:$urn){ assertions(start:0,count:50){ assertions{
      info{ datasetAssertion{ nativeType fields{ path } } }
      runEvents(status:COMPLETE, limit:200){ runEvents{ partitionSpec{partition}
        result{ type nativeResults{key value} } } } } } } }"""
    req = urllib.request.Request(
        GQL,
        data=json.dumps({"query": q, "variables": {"urn": urn}}).encode(),
        headers={"Content-Type": "application/json"},
    )
    data = json.load(urllib.request.urlopen(req))["data"]["dataset"]  # noqa: S310 (local GMS)
    out = []
    for a in (data or {}).get("assertions", {}).get("assertions", []):
        da = a["info"]["datasetAssertion"]
        col = (da.get("fields") or [{}])[0].get("path") if da.get("fields") else None
        for e in a["runEvents"]["runEvents"]:
            native = {kv["key"]: kv["value"] for kv in e["result"].get("nativeResults") or []}
            out.append(
                {"check": da["nativeType"], "column": col, "result": e["result"]["type"],
                 "partition": e["partitionSpec"]["partition"], "ledger_id": native.get("ledger_id")}
            )  # fmt: skip
    return out


def step_catalog(c: Ctx) -> None:
    print("7. DataHub: the gate's results are assertions on the landing dataset")
    pub = subprocess.run(["make", "catalog-quality"], cwd=REPO, capture_output=True, text=True)
    if not check(pub.returncode == 0, "make catalog-quality", pub.stdout.strip().splitlines()[-1]):
        print(pub.stdout[-800:], pub.stderr[-800:])
        return
    ids = {
        tag: str(c.ledger(f"p14-{tag}-{c.stamp}")[-1][0]) for tag in ("bad", "name", "good", "real")
    }
    want = [
        ("bad", "not_null", "order_id", "FAILURE"),
        ("bad", "range", "review_score", "FAILURE"),
        ("bad", "completeness.row_count", None, "SUCCESS"),
        ("name", "naming.filename_pattern", None, "FAILURE"),
        ("good", "not_null", "order_id", "SUCCESS"),
        ("real", "range", "review_score", "SUCCESS"),
    ]
    runs: list[dict] = []
    for _ in range(30):  # timeseries results are indexed asynchronously
        runs = _assertion_runs()
        seen = {(r["ledger_id"], r["check"], r["column"], r["result"]) for r in runs}
        if all((ids[t], ch, col, res) in seen for t, ch, col, res in want):
            break
        time.sleep(2)
    seen = {(r["ledger_id"], r["check"], r["column"], r["result"]): r["partition"] for r in runs}
    for tag, ch, col, res in want:
        part = seen.get((ids[tag], ch, col, res))
        label = f"{ch}({col})" if col else ch
        check(part is not None, f"DataHub: {tag} delivery -> {label} {res}", part or "not found")


def main() -> int:
    parser = argparse.ArgumentParser(description="Proof P14 — landing lifecycle")
    parser.add_argument("--skip-catalog", action="store_true", help="skip the DataHub step")
    args = parser.parse_args()
    c = Ctx()
    try:
        psycopg.connect(c.settings.analytics_loader.conninfo()).close()
    except psycopg.OperationalError as e:
        sys.exit(f"analytics warehouse unreachable — `make up` first ({e})")
    if not c.inbox_empty():
        sys.exit("the reviews inbox is not empty — drain it (`dgp-ingest load-raw`) before P14")
    step_layout(c)
    step_bad_data(c)
    step_bad_name(c)
    step_good(c)
    step_real_and_resend(c)
    step_access(c)
    if not args.skip_catalog:
        step_catalog(c)
    if failures:
        print(f"\nP14 FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print(
        "\nP14 proven: a partner's file is filed to accepted/ (locked, loaded) or quarantine/ "
        "(with its reason, not loaded), the inbox is always drained, access is least-privilege, "
        "and every rule's result is visible in DataHub."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
