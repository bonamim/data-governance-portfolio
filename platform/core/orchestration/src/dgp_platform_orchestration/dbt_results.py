"""Send a dbt invocation's test results to the catalog right after it ran (ADR-0018).

Shared by the Airflow transform DAG (a Cosmos callback on every dbt task) and the CLI publish path
(``wap.py``), so both report the same results. The catalog's ``publish_dbt_results`` does the
DataHub work, run as a module as the ingestion DAG runs ``publish_landing_quality``.

The catalog is a read model: an unreachable DataHub is logged, never fails the dbt task, so it
cannot retry a build or quarantine a model that published.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("dgp.dbt_results")

MODULE = "dgp_platform_catalog.publish_dbt_results"


def has_test_results(target: Path) -> bool:
    """True if the invocation that wrote ``target`` ran tests (a run or run-operation did not)."""
    try:
        results = json.loads((target / "run_results.json").read_text())["results"]
    except (OSError, ValueError, KeyError):
        return False
    return any(r["unique_id"].startswith("test.") for r in results)


def publish(target: Path, stage: str = "") -> None:
    """Publish the test results in the dbt ``target`` dir, if it has any."""
    if not has_test_results(target):
        return
    cmd = [sys.executable, "-m", MODULE, str(target), *(["--stage", stage] if stage else [])]
    done = subprocess.run(cmd, capture_output=True, text=True)
    if done.returncode:
        log.warning("dbt test results not published to the catalog:\n%s", done.stderr.strip())
    else:
        log.info(done.stdout.strip())


def cosmos_callback(project_dir: str, stage: str = "", **_: object) -> None:
    """Cosmos calls this after each dbt command, failed or not, with the task's project copy."""
    publish(Path(project_dir) / "target", stage)


if __name__ == "__main__":
    # make transform: python -m dgp_platform_orchestration.dbt_results <dbt target dir>
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    publish(Path(sys.argv[1]))
