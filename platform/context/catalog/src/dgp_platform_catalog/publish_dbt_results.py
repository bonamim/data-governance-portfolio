"""Publish one dbt invocation's test results into DataHub as assertion run events (ADR-0018).

The dbt recipe (``recipes/dbt.yml``) registers every dbt test as an assertion. The results come
from the run that executed the tests, right after it ran, not from whatever ``run_results.json`` is
left in ``target/`` at ingestion time (any later dbt command overwrites it):

  * the Airflow transform DAG calls this from each Cosmos task, with the task's temporary
    ``target/`` (``dgp_platform_orchestration.dbt_results``). The WAP ``audit`` task passes
    ``--stage audit``.
  * ``make publish`` and ``make transform`` call it after each ``dbt build`` / ``dbt test``.

The assertion URN is the one the DataHub dbt source derives from the test's unique_id, and the
assertee is the tested model's Postgres dataset, so each event lands on the assertion the recipe
registered. Statuses map as the dbt source maps them: pass -> SUCCESS, warn -> SUCCESS, fail ->
FAILURE, error -> ERROR. A WAP audit tests the candidate before it is promoted, so its events are
marked ``stage: audit``. A failed audit means the model was quarantined and consumers kept the last
good version.

Run: ``python -m dgp_platform_catalog.publish_dbt_results <dbt target dir> [--stage audit]``
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from dgp_platform_catalog.gms import default_url

DBT_PLATFORM = "dbt"
# The warehouse the dbt models are materialized in (the recipe's target_platform).
TARGET_PLATFORM = "postgres"
TESTED = ("model", "source", "seed", "snapshot")


@dataclass(frozen=True)
class DbtTestResult:
    """One dbt test result, against one tested node."""

    unique_id: str
    upstream_id: str
    dataset: str  # <database>.<schema>.<relation>, as the recipe names it
    multi_upstream: bool  # the dbt source keys the assertion by upstream too
    status: str
    timestamp_ms: int
    invocation_id: str
    native: dict[str, str] = field(default_factory=dict)


def _relation(node: dict) -> str:
    name = node.get("identifier") if node["resource_type"] == "source" else node.get("alias")
    return f"{node['database']}.{node['schema']}.{name or node['name']}"


def _millis(timestamp: str) -> int:
    return int(datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp() * 1000)


def dbt_test_results(run_results: dict, manifest: dict, stage: str = "") -> list[DbtTestResult]:
    """The test results in ``run_results``, each resolved to the dataset it tested."""
    nodes = {**manifest.get("nodes", {}), **manifest.get("sources", {})}
    meta = run_results["metadata"]
    runs = []
    for result in run_results.get("results", []):
        uid = result["unique_id"]
        if not uid.startswith("test.") or uid not in nodes:
            continue
        upstreams = [
            u for u in nodes[uid]["depends_on"]["nodes"] if u.split(".")[0] in TESTED and u in nodes
        ]
        started = next(
            (t["started_at"] for t in result.get("timing", []) if t["name"] == "execute"), None
        )
        native = {} if result["status"] == "pass" else {"message": result.get("message") or ""}
        if result.get("failures"):
            native["failures"] = str(result["failures"])
        if stage:
            native["stage"] = stage
        for upstream in upstreams:
            runs.append(
                DbtTestResult(
                    unique_id=uid,
                    upstream_id=upstream,
                    dataset=_relation(nodes[upstream]),
                    multi_upstream=len(upstreams) > 1,
                    status=result["status"],
                    timestamp_ms=_millis(started or meta["generated_at"]),
                    invocation_id=meta["invocation_id"],
                    native=native,
                )
            )
    return runs


def _mcps(runs: list[DbtTestResult]) -> list:
    # The DataHub SDK is the optional ``publish`` extra, so the parsing above works without it.
    from datahub.emitter.mce_builder import datahub_guid, make_assertion_urn, make_dataset_urn
    from datahub.emitter.mcp import MetadataChangeProposalWrapper
    from datahub.metadata.schema_classes import (
        AssertionResultClass,
        AssertionResultSeverityClass,
        AssertionResultTypeClass,
        AssertionRunEventClass,
        AssertionRunStatusClass,
    )

    result_of = {
        "pass": (AssertionResultTypeClass.SUCCESS, None),
        "warn": (AssertionResultTypeClass.SUCCESS, None),
        "error": (AssertionResultTypeClass.ERROR, None),
    }
    mcps = []
    for r in runs:
        guid = {"platform": DBT_PLATFORM, "name": r.unique_id}
        if r.multi_upstream:
            guid["on_dbt_upstream"] = r.upstream_id
        urn = make_assertion_urn(datahub_guid(guid))
        kind, severity = result_of.get(
            r.status, (AssertionResultTypeClass.FAILURE, AssertionResultSeverityClass.HIGH)
        )
        mcps.append(
            MetadataChangeProposalWrapper(
                entityUrn=urn,
                aspect=AssertionRunEventClass(
                    timestampMillis=r.timestamp_ms,
                    assertionUrn=urn,
                    asserteeUrn=make_dataset_urn(TARGET_PLATFORM, r.dataset),
                    runId=r.invocation_id,
                    status=AssertionRunStatusClass.COMPLETE,
                    result=AssertionResultClass(
                        type=kind, severity=severity, nativeResults=r.native
                    ),
                ),
            )
        )
    return mcps


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish dbt test results into DataHub.")
    parser.add_argument("target", type=Path, help="the dbt target dir of the invocation")
    parser.add_argument("--stage", default="", help="'audit' for a WAP audit of the candidate")
    args = parser.parse_args(argv)

    run_results = json.loads((args.target / "run_results.json").read_text())
    manifest = json.loads((args.target / "manifest.json").read_text())
    runs = dbt_test_results(run_results, manifest, args.stage)
    if not runs:
        print(f"no dbt test results in {args.target}")
        return 0

    from datahub.emitter.rest_emitter import DatahubRestEmitter

    emitter = DatahubRestEmitter(default_url())
    for mcp in _mcps(runs):
        emitter.emit_mcp(mcp)
    failed = sum(r.status not in ("pass", "warn") for r in runs)
    print(f"published {len(runs)} dbt test results ({failed} not passing) from {args.target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
