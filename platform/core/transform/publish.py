#!/usr/bin/env python3
"""Write-audit-publish orchestrator for the CLI (ADR-0014/0015, proof P3).

Runs the same per-model steps as the Airflow transform DAG (``dgp_platform_orchestration.wap``);
all publish SQL lives in the ``dgp_dbt`` package macros. For each domain, in dependency order:

  1. core           ``dbt build`` of everything except opted-in models and their descendants
  2. per WAP model  ``wap_prepare`` → ``dbt build`` to ``<name>__candidate`` (WRITE + AUDIT: the
                    contract DQ runs in the build) → pass: ``wap_promote`` / fail:
                    ``wap_drop_candidate`` (QUARANTINE: consumers keep the last good version)
  3. descendants    of promoted models (those of a quarantined one keep its last good version)
  4. masking + consumer grants; on success the manifest is saved as dbt state (Slim CI)

Opt-in is per data product (``publish: {audit: required}``). A microbatch model is audited and
merged per batch window: by default the latest batches, or ``--event-time-start/--end`` for a
backfill. ``--full-refresh`` rebuilds incremental models in full.
Run: ``make publish`` (or ``make backfill DOMAIN=sales START=2017-01-01 END=2017-03-01``).
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from dgp_platform_orchestration import dbt_state, wap

REPO_ROOT = Path(__file__).resolve().parents[3]


def _grant() -> None:
    """Re-apply masking views, then consumer grants.

    dbt's view and table materializations swap relations and drop the old one with CASCADE —
    taking the role-aware masking views (ADR-0013) and grants with them. So masking is re-applied
    before grants after every domain.
    """
    access = REPO_ROOT / "platform" / "access"
    subprocess.run([sys.executable, str(access / "apply_masking.py")], check=False)
    subprocess.run([sys.executable, str(access / "apply_grants.py")], check=False)


def _date(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Write-audit-publish output ports.")
    parser.add_argument("--domain", help="publish only this domain (default: all, in order)")
    parser.add_argument("--event-time-start", help="backfill window start (microbatch models)")
    parser.add_argument("--event-time-end", help="backfill window end, exclusive")
    parser.add_argument("--full-refresh", action="store_true", help="rebuild incrementals in full")
    parser.add_argument("--no-state", action="store_true", help="don't save the dbt state")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    exit_code = 0
    for domain in [args.domain] if args.domain else wap.DOMAINS:
        ok = wap.publish_domain(
            REPO_ROOT,
            domain,
            start=_date(args.event_time_start),
            end=_date(args.event_time_end),
            full_refresh=args.full_refresh,
        )
        # Grant after each domain so the next one can read its upstream ports.
        _grant()
        if not ok:
            print(
                f"  QUARANTINED {domain}: an audit failed — its last good version stays published"
            )
            exit_code = 1
        elif not args.no_state:
            dbt_state.save(wap.project_dir(REPO_ROOT, domain) / "target" / "manifest.json", domain)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
