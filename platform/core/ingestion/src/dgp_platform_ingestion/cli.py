"""Command-line entrypoint for the ingestion platform capability.

    dgp-ingest acquire     # download Olist from Kaggle into the local cache
    dgp-ingest seed        # load operational CSVs into shop_oltp
    dgp-ingest land        # PoC partner simulator: deliver files into the landing inbox
    dgp-ingest gate        # drain the landing inbox through the contract gate (accepted/quarantine)
    dgp-ingest load-raw    # load every dataset into <domain>_raw: OLTP + accepted files (+ ledger)
    dgp-ingest replay      # rebuild raw from shop_oltp + archive accepted/ (--partition-date)
    dgp-ingest sla         # check landing delivery deadlines, alert the owner on a miss
    dgp-ingest all         # acquire -> seed -> land -> gate -> load-raw

The Airflow DAGs call the same functions in pipeline.py. This CLI is for local
runs and for the Makefile.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from . import pipeline, sla
from .settings import Settings


def _default_repo_root() -> Path:
    env = os.environ.get("DGP_REPO_ROOT")
    return Path(env).resolve() if env else Path.cwd()


def _default_data_dir(repo_root: Path) -> Path:
    env = os.environ.get("DGP_DATA_DIR")
    return Path(env).resolve() if env else repo_root / "data"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dgp-ingest", description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=None, help="repo root (default: cwd)")
    parser.add_argument("--data-dir", type=Path, default=None, help="data cache dir")
    parser.add_argument("--run-id", default=None, help="run id recorded in the ledger")
    parser.add_argument(
        "--domain", default=None, help="limit seed/land/gate/load-raw to one domain (default: all)"
    )
    parser.add_argument(
        "--partition-date",
        default=None,
        help="business date YYYY-MM-DD: the date `land` delivers for, the partition `replay` "
        "reads (default: today UTC)",
    )
    parser.add_argument(
        "command",
        choices=["acquire", "seed", "land", "gate", "load-raw", "replay", "sla", "all"],
        help="pipeline step to run",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    repo_root = (args.repo_root or _default_repo_root()).resolve()
    data_dir = (args.data_dir or _default_data_dir(repo_root)).resolve()
    run_id = args.run_id or f"local-{int(time.time())}"
    settings = Settings.from_env()

    steps = {
        "acquire": lambda: pipeline.acquire(repo_root, settings, data_dir),
        "seed": lambda: pipeline.seed_oltp(repo_root, settings, data_dir, args.domain),
        "land": lambda: pipeline.land_files(
            repo_root, settings, data_dir, args.domain, args.partition_date
        ),
        "gate": lambda: pipeline.contract_gate(repo_root, settings, run_id, args.domain),
        "load-raw": lambda: pipeline.load_raw(repo_root, settings, run_id, args.domain),
        "sla": lambda: [
            print(
                f"{f.dataset} {f.partition}: {f.state} (deadline {f.deadline:%Y-%m-%d %H:%M} UTC)"
                + (f": {f.detail}" if f.detail else "")
            )
            for f in sla.check(repo_root, settings, args.domain)
        ],
        "replay": lambda: pipeline.replay(
            repo_root, settings, run_id, args.partition_date or pipeline._today_utc(), args.domain
        ),
    }
    order = (
        ["acquire", "seed", "land", "gate", "load-raw"] if args.command == "all" else [args.command]
    )
    for step in order:
        steps[step]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
