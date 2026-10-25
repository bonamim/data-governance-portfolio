"""Landing delivery deadlines (ADR-0016): is each dataset's due partition in on time?

A landing contract declares when a partition must be available
(``slaProperties.timeOfAvailability``) and how far the business date trails it (``latency`` in
days): with 04:00 UTC and one day, dt=D-1 is due by 04:00 on day D. Once a deadline has passed,
the due partition is:

  delivered   a file of it was accepted (or skipped as an unchanged re-delivery) — nothing to do
  incomplete  files of it are in the inbox, waiting on their completion signal (expected_files /
              marker): the partner sent part of the batch
  rejected    files of it arrived but every one was quarantined
  missing     nothing arrived

Anything but ``delivered`` alerts the owning domain once per dataset and partition.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from dgp_platform_quality.rules import load_contract_for

from . import alerts, arrivals, db, layout, ledger
from .config import load_datasets
from .layout import LandingRules
from .objectstore import ObjectStore
from .settings import Settings

log = logging.getLogger("dgp.sla")


@dataclass(frozen=True)
class Finding:
    domain: str
    dataset: str
    partition: str
    deadline: datetime
    state: str  # delivered | incomplete | rejected | missing
    detail: str = ""


def classify(statuses: set[str], waiting_files: int) -> str:
    """The due partition's state from its ledger statuses and the files waiting in the inbox."""
    if statuses & {"accepted", "loaded", "skipped", "superseded"}:
        return "delivered"
    if waiting_files:
        return "incomplete"
    if "quarantined" in statuses:
        return "rejected"
    return "missing"


def check(
    repo_root: Path, settings: Settings, domain: str | None = None, now: datetime | None = None
) -> list[Finding]:
    """Check every landing dataset's due partition; alert the owner on a miss. Returns findings."""
    now = now or datetime.now(UTC)
    store = ObjectStore(settings.objectstore)
    findings = []
    with db.connect(settings.analytics_loader) as conn:
        ledger.ensure_schema(conn)
        for d in (x for x in load_datasets(repo_root, domain) if x.source_type == "landing"):
            rules = LandingRules.from_contract(load_contract_for(repo_root, d.domain, d.name))
            due = rules.due_partition(now)
            if due is None:
                continue
            partition, deadline = due
            dataset_id = f"{d.source_system}/{d.name}"
            statuses = {
                r[0]
                for r in conn.execute(
                    f"SELECT DISTINCT status FROM {ledger.LEDGER} WHERE dataset = %s "
                    "AND partition = %s",
                    (dataset_id, partition),
                ).fetchall()
            }
            inbox = store.list(store.inbox, layout.inbox_prefix(d.source_system, d.name))
            _, waiting = layout.plan_batches(rules, [o for o in inbox if not o.key.endswith("/")],
                                             now)  # fmt: skip
            waiting_files = sum(len(b.files) for b in waiting if b.partition == partition)
            state = classify(statuses, waiting_files)
            detail = {
                "incomplete": f"{waiting_files} file(s) waiting on the {rules.completion_signal} "
                "completion signal",
                "rejected": "every file delivered for it was quarantined",
                "missing": "no file arrived",
            }.get(state, "")
            f = Finding(d.domain, dataset_id, partition, deadline, state, detail)
            findings.append(f)
            if state != "delivered":
                alerts.send(
                    conn, repo_root, domain=d.domain, kind=f"landing.sla.{state}",
                    subject=f"{dataset_id} {partition} {state} at its {deadline:%H:%M} UTC "
                    f"deadline — {detail}",
                    dedup_key=f"landing.sla:{dataset_id}:{partition}",
                    detail={"partition": partition, "deadline": deadline.isoformat(),
                            "state": state},
                )  # fmt: skip
            log.info("%s %s: %s (deadline %s)", dataset_id, partition, state, deadline)
    if settings.landing_events is not None:
        with db.connect(settings.landing_events) as q:
            arrivals.purge(q)  # rows no dataset prefix will ever ack
    return findings
