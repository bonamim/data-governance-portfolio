"""Runtime context for DAG tasks inside the Airflow image.

The repo is mounted read-only and the Olist cache lives on a shared volume so the
acquire task and the domain tasks see the same files.
"""

from __future__ import annotations

import os
from pathlib import Path

from dgp_platform_ingestion.settings import Settings


def repo_root() -> Path:
    return Path(os.environ.get("DGP_REPO_ROOT", "/opt/airflow/repo")).resolve()


def data_dir() -> Path:
    return Path(os.environ.get("DGP_DATA_DIR", "/opt/airflow/data")).resolve()


def settings() -> Settings:
    return Settings.from_env()
