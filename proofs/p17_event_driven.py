#!/usr/bin/env python3
"""Proof P17 — event-driven ingestion: an upload runs ingestion, a burst runs it once (ADR-0016).

Nobody triggers a DAG here. A partner uploads into the landing inbox; the object store notifies the
arrival queue; Airflow's event trigger fires ``landing/customer_experience``; the domain's ingestion
DAG gates and loads.

  1. burst      the partner uploads 5 files within a second: one good file sent with its own
                ``x-amz-checksum-sha256``, and 4 misnamed ones
  2. fan-in     all 5 are decided by ONE arrival-triggered run (windowed: no DAG run per file)
  3. outcome    the good file is accepted (its checksum verified) and loaded; the 4 are
                quarantined, each alerting the owning domain; the inbox is drained and the
                queue rows acked
  4. freshness  DataHub shows the delivery as an ARRIVAL operation at the time it landed
  5. restore    the real Olist file, delivered the same way, is loaded back
  6. deadlines  the platform_landing_sla DAG runs and reports every dataset

Needs the platform and Airflow up (``make airflow-up``); DataHub optional. Run: ``make prove-p17``.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
import psycopg

REPO = Path(__file__).resolve().parents[1]
SCHED = "governance-platform-airflow-scheduler-1"
DAG = "customer_experience_ingestion"
PREFIX = "olist/reviews/"
REAL = REPO / "data" / "olist" / "olist_order_reviews_dataset.csv"
GQL = f"http://localhost:{os.environ.get('DATAHUB_MAPPED_GMS_PORT', '8081')}/api/graphql"
HEADER = ["review_id", "order_id", "review_score", "review_comment_title",
          "review_comment_message", "review_creation_date", "review_answer_timestamp"]  # fmt: skip

failures: list[str] = []


def check(ok: bool, what: str, detail: str = "") -> bool:
    print(f"  {'OK  ' if ok else 'FAIL'} {what}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(what)
    return ok


def sql(query: str, *args):
    with psycopg.connect(
        f"host=localhost port={os.environ.get('ANALYTICS_PORT', '5433')} "
        f"dbname={os.environ.get('ANALYTICS_DB', 'analytics')} "
        f"user=platform_loader password={os.environ['PLATFORM_LOADER_PASSWORD']}"
    ) as c:
        cur = c.execute(query, args)
        return cur.fetchall() if cur.description else []


def queue_rows(keys: list[str]) -> int:
    with psycopg.connect(
        f"host=localhost port={os.environ.get('ANALYTICS_PORT', '5433')} "
        f"dbname={os.environ.get('ANALYTICS_DB', 'analytics')} "
        f"user=landing_events password={os.environ['LANDING_EVENTS_PASSWORD']}"
    ) as c:
        rows = c.execute("SELECT event_data #>> '{Records,0,s3,object,key}' FROM "
                         "landing_events.arrivals").fetchall()  # fmt: skip
    from urllib.parse import unquote_plus

    return sum(1 for (k,) in rows if unquote_plus(k) in keys)


def partner():
    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("MINIO_ENDPOINT", "http://localhost:9000"),
        aws_access_key_id="partner-olist",
        aws_secret_access_key=os.environ["LANDING_PARTNER_OLIST_SECRET"],
        region_name="us-east-1",
    )


def _csv(n: int = 1001) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(HEADER)
    stamp = f"{time.time():.0f}"  # unique per run, so a rerun is never an unchanged re-delivery
    w.writerows(
        [[f"p17r{stamp}_{i}", f"p17o{i}", "5", "", "ok", "2025-02-01", ""] for i in range(n)]
    )
    return buf.getvalue().encode()


def airflow(*args: str) -> str:
    return subprocess.run(["docker", "exec", SCHED, "airflow", *args],
                          capture_output=True, text=True).stdout  # fmt: skip


def _json(out: str):
    m = re.search(r"^\[(\{|\])", out, re.M)
    return json.loads(out[m.start() :]) if m else []


def gated(keys: list[str], since: datetime, timeout: int = 240) -> list[tuple]:
    """Ledger gate rows for ``keys`` (waits until every key has one)."""
    rows: list[tuple] = []
    for _ in range(timeout // 5):
        rows = sql(
            "SELECT source_identifier, status, run_id, checks FROM platform_meta.ingestion_ledger"
            " WHERE source_identifier = ANY(%s) AND loaded_at >= %s AND checks IS NOT NULL",
            keys,
            since,
        )
        if {r[0] for r in rows} >= set(keys):
            return rows
        time.sleep(5)
    return rows


def wait_run(run_id: str, timeout: int = 240) -> str:
    for _ in range(timeout // 5):
        state = next((r["state"] for r in _json(airflow("dags", "list-runs", DAG, "-o", "json"))
                      if r["run_id"] == run_id), "")  # fmt: skip
        if state in ("success", "failed"):
            return state
        time.sleep(5)
    return "timeout"


def step_burst() -> tuple[str, list[str], datetime, datetime]:
    print("1. Burst: the partner uploads 5 files within a second (nobody triggers a DAG)")
    p = partner()
    good = f"{PREFIX}olist_order_reviews_20250201.csv"
    body = _csv()
    junk = [f"{PREFIX}p17_junk_{i}.csv" for i in range(4)]
    since = datetime.now(UTC) - timedelta(seconds=1)
    sent = base64.b64encode(hashlib.sha256(body).digest()).decode()
    p.put_object(Bucket="landing-inbox", Key=good, Body=body, ChecksumSHA256=sent)
    put_at = datetime.now(UTC)
    for k in junk:
        p.put_object(Bucket="landing-inbox", Key=k, Body=b"not,a,review\n")
    check(True, "5 files uploaded by the partner", f"{good.rsplit('/', 1)[1]} + 4 misnamed")
    return good, junk, since, put_at


def step_fan_in(good: str, junk: list[str], since: datetime) -> str:
    print("2. Fan-in: one arrival-triggered run decides all 5")
    rows = gated([good, *junk], since)
    runs = {r[2] for r in rows}
    check(len(rows) == 5, "all 5 files gated", f"{len(rows)} ledger rows")
    check(len(runs) == 1, "by a single run (windowed, not one run per file)", ", ".join(runs))
    run_id = next(iter(runs)) if runs else ""
    check(run_id.startswith("asset_triggered__"), "the run was fired by the arrival asset", run_id)
    return run_id


def step_outcome(run_id: str, good: str, junk: list[str], since: datetime) -> None:
    print("3. Outcome: accepted + verified + loaded; misnamed quarantined and alerted; acked")
    state = wait_run(run_id)
    check(state == "success", "the ingestion run succeeded", state)
    rows = {r[0]: r for r in gated([good, *junk], since, 10)}
    status, checks = rows[good][1], rows[good][3]
    check(status == "accepted", "good file accepted", status)
    integrity = [c for c in checks if c["check"] == "integrity.checksum_sha256"]
    check(bool(integrity) and integrity[0]["passed"], "the partner's SHA-256 was verified",
          integrity[0]["detail"] if integrity else "no integrity check")  # fmt: skip
    loaded = sql("SELECT row_count FROM platform_meta.ingestion_ledger WHERE source_identifier = %s"
                 " AND status = 'loaded' AND run_id = %s", good, run_id)  # fmt: skip
    check(bool(loaded) and loaded[0][0] == 1001, "loaded into raw by the same run",
          f"{loaded[0][0] if loaded else 0} rows")  # fmt: skip
    check(all(rows[k][1] == "quarantined" for k in junk), "the 4 misnamed files quarantined")
    alerts = sql(
        "SELECT count(*) FROM platform_meta.alerts WHERE kind = 'landing.quarantine'"
        " AND domain = 'customer_experience' AND raised_at >= %s",
        since,
    )[0][0]
    check(alerts == 4, "each quarantine alerted the owning domain", f"{alerts} alerts")
    loader = boto3.client(
        "s3", endpoint_url=os.environ.get("MINIO_ENDPOINT", "http://localhost:9000"),
        aws_access_key_id=os.environ["LANDING_LOADER_USER"],
        aws_secret_access_key=os.environ["LANDING_LOADER_SECRET_KEY"], region_name="us-east-1",
    )  # fmt: skip
    left = loader.list_objects_v2(Bucket="landing-inbox", Prefix=PREFIX).get("KeyCount", 0)
    check(left == 0, "the inbox is drained", f"{left} objects left")
    for _ in range(24):
        if queue_rows([good, *junk]) == 0:
            break
        time.sleep(5)
    check(queue_rows([good, *junk]) == 0, "their arrival-queue rows were acked")


def step_freshness(good: str, put_at: datetime) -> None:
    print("4. Freshness: DataHub records the delivery at the time it landed")
    urn = "urn:li:dataset:(urn:li:dataPlatform:s3,landing-archive/accepted/olist/reviews,PROD)"
    q = ('{ dataset(urn:"' + urn + '"){ operations(limit:20){ lastUpdatedTimestamp '
         'customOperationType customProperties{key value} } } }')  # fmt: skip
    try:
        req = urllib.request.Request(GQL, data=json.dumps({"query": q}).encode(),
                                     headers={"Content-Type": "application/json"})  # fmt: skip
        ops = json.load(urllib.request.urlopen(req))["data"]["dataset"]["operations"]  # noqa: S310
    except OSError:
        print("  (DataHub not running — skipped)")
        return
    mine = [o for o in ops if {"key": "file", "value": f"s3://landing-inbox/{good}"}
            in (o["customProperties"] or [])]  # fmt: skip
    if not check(bool(mine), "an ARRIVAL operation for the delivered file"):
        return
    landed = datetime.fromtimestamp(mine[0]["lastUpdatedTimestamp"] / 1000, UTC)
    check(abs((landed - put_at).total_seconds()) < 5, "stamped with the upload time",
          f"{landed:%H:%M:%S} vs upload {put_at:%H:%M:%S}")  # fmt: skip


def step_restore(since: datetime) -> None:
    print("5. Restore: the real Olist file, delivered the same way, is loaded back")
    if not REAL.exists():
        check(False, f"{REAL} present", "run `make acquire` first")
        return
    key = f"{PREFIX}olist_order_reviews_{(datetime.now(UTC) - timedelta(days=1)):%Y%m%d}.csv"
    partner().upload_file(str(REAL), "landing-inbox", key)
    rows = gated([key], since)
    run = rows[0][2] if rows else ""
    check(bool(rows) and wait_run(run) == "success", "arrival-triggered run loaded it", run)
    count = sql("SELECT count(*) FROM customer_experience_raw.reviews")[0][0]
    check(count > 99000, "raw holds the real reviews again", f"{count} rows")


def step_deadlines() -> None:
    print("6. Deadlines: the SLA monitor checks every landing dataset")
    out = _json(airflow("dags", "trigger", "platform_landing_sla", "-o", "json"))
    run_id = out[0]["dag_run_id"] if out else ""
    for _ in range(36):
        runs = _json(airflow("dags", "list-runs", "platform_landing_sla", "-o", "json"))
        state = next((r["state"] for r in runs if r["run_id"] == run_id), "")
        if state in ("success", "failed"):
            break
        time.sleep(5)
    check(state == "success", "platform_landing_sla ran", state)


def main() -> int:
    if subprocess.run(["docker", "inspect", SCHED], capture_output=True).returncode:
        sys.exit("Airflow is not running — `make airflow-up` first")
    for dag in (DAG, "platform_landing_sla"):  # new DAGs start paused
        airflow("dags", "unpause", dag)
    good, junk, since, put_at = step_burst()
    run_id = step_fan_in(good, junk, since)
    if run_id:
        step_outcome(run_id, good, junk, since)
        step_freshness(good, put_at)
    step_restore(since)
    step_deadlines()
    if failures:
        print(f"\nP17 FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print(
        "\nP17 proven: a partner's upload alone runs the pipeline — one run per burst, every file"
        " gated, verified, alerted on and acked — and DataHub dates it from its arrival."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
