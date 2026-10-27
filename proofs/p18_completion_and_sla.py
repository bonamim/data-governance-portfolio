#!/usr/bin/env python3
"""Proof P18: batch completion signals, delivery deadlines and alerts (ADR-0016).

The reviews contract is switched (temporarily) to multi-file deliveries, and a local HTTP endpoint
stands in for the domain's alert webhook (``ALERT_WEBHOOK_CUSTOMER_EXPERIENCE``):

  1. expected_files  expectedFiles: 2. One part of a partition waits, and past its deadline the SLA
                     monitor reports it INCOMPLETE and alerts once (a re-check does not re-alert).
                     the second part completes the batch: both accepted together, loaded together
  2. bad batch       one good + one contract-violating part of a partition: the WHOLE batch is
                     quarantined with one _reason.json naming the bad file, and the owner alerted
  3. marker          completionMarker _DONE_<date>: data waits until the marker lands, then it is
                     accepted and the marker consumed
  4. missing         a partition nothing arrived for is reported MISSING at its deadline
  5. restore         the contract back to per_file, and the real Olist file is loaded again

The domain's ingestion DAG is paused while this runs (the proof drives the gate itself).
Run: ``make prove-p18``.
"""

from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

from _checks import Checks, object_store
from dgp_governance.repo import root
from dgp_platform_ingestion import db, layout, pipeline, sla
from dgp_platform_ingestion.settings import Settings

REPO = root()
CONTRACT = REPO / "domains/customer_experience/contracts/landing/reviews.odcs.yaml"
DOMAIN, PREFIX = "customer_experience", "olist/reviews/"
REAL = REPO / "data" / "olist" / "olist_order_reviews_dataset.csv"
SCHED = "governance-platform-airflow-scheduler-1"
HEADER = [
    "review_id",
    "order_id",
    "review_score",
    "review_comment_title",
    "review_comment_message",
    "review_creation_date",
    "review_answer_timestamp",
]
PER_FILE = "    value: '^olist_order_reviews_(?P<date>\\d{8})\\.csv$'\n  - property: completionSignal\n    value: per_file\n"  # noqa: E501

check = Checks("P18")
received: list[dict] = []

# Fresh business dates each run (a past range keyed on the clock), so a rerun never meets the
# previous run's ledger rows or alert dedup keys.
BASE = datetime(2000, 1, 1, tzinfo=UTC) + timedelta(days=7 * (int(time.time()) // 60 % 1300))


def day(offset: int) -> datetime:
    return BASE + timedelta(days=offset)


def name(offset: int, part: str = "") -> str:
    return f"olist_order_reviews_{day(offset):%Y%m%d}{part}.csv"


def dt(offset: int) -> str:
    return f"dt={day(offset):%Y-%m-%d}"


class _Hook(BaseHTTPRequestHandler):
    def do_POST(self):
        received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        pass


def _signal(text: str) -> str:
    """The contract's completion block for a batch signal (file names may carry a _part<n>)."""
    return (
        "    value: '^olist_order_reviews_(?P<date>\\d{8})(_part\\d+)?\\.csv$'\n"
        f"  - property: completionSignal\n{text}"
    )


def _csv(tag: str, bad: bool = False) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(HEADER)
    rows = [
        [f"p18{tag}r{i}", f"p18{tag}o{i}", "4", "", "ok", "2025-03-01", ""] for i in range(1001)
    ]
    if bad:
        rows[0][1] = ""  # order_id missing
    w.writerows(rows)
    return buf.getvalue().encode()


class Ctx:
    def __init__(self):
        self.settings = Settings.from_env()
        self.partner = object_store("partner-olist", os.environ["LANDING_PARTNER_OLIST_SECRET"])
        self.n = 0
        self.stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")

    def put(self, name: str, body: bytes) -> None:
        self.partner.put_object(Bucket="landing-inbox", Key=PREFIX + name, Body=body)

    def gate(self) -> dict:
        self.n += 1
        return pipeline.contract_gate(REPO, self.settings, f"p18-{self.stamp}-{self.n}", DOMAIN)

    def load(self) -> dict:
        return pipeline.load_raw(REPO, self.settings, f"p18-{self.stamp}-load{self.n}", DOMAIN)

    def sla(self, on: datetime) -> sla.Finding:
        now = on.replace(hour=5)  # past the 04:00 UTC deadline
        (f,) = [
            x for x in sla.check(REPO, self.settings, DOMAIN, now) if x.dataset.endswith("reviews")
        ]
        return f

    def ledger(self, partition: str) -> list[tuple]:
        with db.connect(self.settings.analytics_loader) as c:
            return c.execute(
                "SELECT source_identifier, status, batch_id, archive_key FROM"
                " platform_meta.ingestion_ledger WHERE dataset = 'olist/reviews' AND partition = %s"
                " AND run_id LIKE %s ORDER BY id",
                (partition, f"p18-{self.stamp}%"),
            ).fetchall()

    def raw_count(self) -> int:
        with db.connect(self.settings.analytics_loader) as c:
            return c.execute(f"SELECT count(*) FROM {DOMAIN}_raw.reviews").fetchone()[0]


def alerts_of(kind: str) -> list[dict]:
    return [a for a in received if a["kind"] == kind]


def step_expected_files(c: Ctx) -> None:
    print("1. expected_files: a partial batch waits, is reported incomplete, then completes")
    c.put(name(0, "_part1"), _csv("a1"))
    out = c.gate()
    check(out["waiting"] == 1 and out["accepted"] == 0, "part 1 of 2 waits in the inbox", str(out))
    f = c.sla(day(1))
    check(
        f.partition == dt(0) and f.state == "incomplete",
        "past the 04:00 deadline the partition is INCOMPLETE",
        f"{f.state}: {f.detail}",
    )
    hook = alerts_of("landing.sla.incomplete")
    check(
        len(hook) == 1 and hook[0]["channel"] == "#cx-data" and dt(0) in hook[0]["text"],
        "the owner's webhook got the alert",
        hook[0]["text"] if hook else "nothing received",
    )
    c.sla(day(1))
    check(len(alerts_of("landing.sla.incomplete")) == 1, "a re-check does not alert again")
    c.put(name(0, "_part2"), _csv("a2"))
    out = c.gate()
    check(out["accepted"] == 2, "part 2 completes the batch: both accepted", str(out))
    rows = c.ledger(dt(0))
    check(
        len({r[2] for r in rows if r[1] == "accepted"}) == 1,
        "decided as ONE batch",
        ", ".join(sorted({r[2] for r in rows})),
    )
    out = c.load()
    check(
        out["loaded"] == 2 and c.raw_count() == 2002,
        "loaded together (2 files, one table)",
        f"{c.raw_count()} rows",
    )
    check(c.sla(day(1)).state == "delivered", "the partition is now delivered")


def step_bad_batch(c: Ctx) -> None:
    print("2. Bad batch: one bad part quarantines the whole batch, with one reason")
    c.put(name(2, "_part1"), _csv("b1"))
    c.put(name(2, "_part2"), _csv("b2", bad=True))
    out = c.gate()
    check(out["quarantined"] == 2 and out["accepted"] == 0, "both files quarantined", str(out))
    rows = c.ledger(dt(2))
    good = [r for r in rows if r[0].endswith("part1.csv")]
    reason_key = rows[0][3].rsplit("/", 1)[0] + "/" + layout.REASON_FILE
    root = object_store(os.environ["MINIO_ROOT_USER"], os.environ["MINIO_ROOT_PASSWORD"])
    reason = json.load(root.get_object(Bucket="landing-archive", Key=reason_key)["Body"])
    check(
        sorted(reason["batch"]["files"]) == [name(2, "_part1"), name(2, "_part2")],
        "_reason.json lists the whole batch",
    )
    check(
        {f["file"] for f in reason["failed"]} == {name(2, "_part2")},
        "and names the failing file",
        "; ".join(f"{f['file']}: {f['check']}({f['column']})" for f in reason["failed"]),
    )
    check(bool(good) and good[0][1] == "quarantined", "the good part is held back with its batch")
    check(len(alerts_of("landing.quarantine")) == 1, "one quarantine alert to the owner")


def step_marker(c: Ctx) -> None:
    print("3. marker: data waits for the partner's marker, which is then consumed")
    text = CONTRACT.read_text(encoding="utf-8").replace(
        _signal("    value: expected_files\n  - property: expectedFiles\n    value: 2\n"),
        _signal(
            "    value: marker\n  - property: completionMarker\n"
            "    value: '^_DONE_(?P<date>\\d{8})$'\n"
        ),
    )
    CONTRACT.write_text(text, encoding="utf-8")
    c.put(name(4, "_part1"), _csv("c1"))
    out = c.gate()
    check(out["waiting"] == 1 and out["accepted"] == 0, "no marker yet: the file waits", str(out))
    c.put(f"_DONE_{day(4):%Y%m%d}", b"")
    out = c.gate()
    check(out["accepted"] == 1, "the marker closes the batch: accepted", str(out))
    loader = object_store(
        os.environ["LANDING_LOADER_USER"], os.environ["LANDING_LOADER_SECRET_KEY"]
    )
    left = loader.list_objects_v2(Bucket="landing-inbox", Prefix=PREFIX).get("KeyCount", 0)
    check(left == 0, "the marker was consumed (inbox empty)", f"{left} left")


def step_missing(c: Ctx) -> None:
    print("4. Missing: nothing arrived for a partition by its deadline")
    f = c.sla(day(6))
    check(
        f.partition == dt(5) and f.state == "missing",
        "reported MISSING",
        f"{f.partition}: {f.state}",
    )
    check(len(alerts_of("landing.sla.missing")) == 1, "the owner was alerted")


def step_restore(c: Ctx, original: str) -> None:
    print("5. Restore: contract back to per_file, the real file loaded again")
    CONTRACT.write_text(original, encoding="utf-8")
    if not REAL.exists():
        check(False, f"{REAL} present", "download it first: `uv run dgp-ingest acquire`")
        return
    yesterday = datetime.now(UTC) - timedelta(days=1)
    key = f"{PREFIX}olist_order_reviews_{yesterday:%Y%m%d}.csv"
    c.partner.upload_file(str(REAL), "landing-inbox", key)
    c.gate()
    c.load()
    check(c.raw_count() > 99000, "raw holds the real reviews again", f"{c.raw_count()} rows")


def _pause(pause: bool) -> None:
    if subprocess.run(["docker", "inspect", SCHED], capture_output=True).returncode == 0:
        subprocess.run(
            [
                "docker",
                "exec",
                SCHED,
                "airflow",
                "dags",
                "pause" if pause else "unpause",
                f"{DOMAIN}_ingestion",
            ],
            capture_output=True,
        )


def main() -> int:
    original = CONTRACT.read_text(encoding="utf-8")
    if PER_FILE not in original:
        sys.exit("the reviews contract is not in its per_file shape. Restore it first")
    server = HTTPServer(("127.0.0.1", 0), _Hook)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    os.environ["ALERT_WEBHOOK_CUSTOMER_EXPERIENCE"] = f"http://127.0.0.1:{server.server_port}/hook"
    c = Ctx()
    _pause(True)
    try:
        CONTRACT.write_text(
            original.replace(
                PER_FILE,
                _signal("    value: expected_files\n  - property: expectedFiles\n    value: 2\n"),
            ),
            encoding="utf-8",
        )
        step_expected_files(c)
        step_bad_batch(c)
        step_marker(c)
        step_missing(c)
        step_restore(c, original)
    finally:
        CONTRACT.write_text(original, encoding="utf-8")
        _pause(False)
        server.shutdown()
    return check.verdict(
        "multi-file deliveries are decided per complete batch (by count or by the "
        "partner's marker), a partial, rejected or missing delivery is caught at its "
        "deadline, and the owning domain is alerted once, on its own route."
    )


if __name__ == "__main__":
    sys.exit(main())
