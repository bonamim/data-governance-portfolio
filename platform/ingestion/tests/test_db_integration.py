"""Integration test for the generic CSV loader against the analytics warehouse.

Skips automatically when the warehouse is not reachable (e.g. `make up` not run),
so the unit suite stays runnable anywhere.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from dgp_platform_ingestion import db  # noqa: E402
from dgp_platform_ingestion.settings import Settings  # noqa: E402


@pytest.fixture(scope="module")
def analytics():
    try:
        settings = Settings.from_env()
        with db.connect(settings.analytics_loader) as conn:
            yield conn
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"analytics warehouse not reachable: {exc}")


def test_load_csv_file_creates_and_loads(tmp_path: Path, analytics):
    csv_path = tmp_path / "widgets.csv"
    csv_path.write_text("id,name\n1,alpha\n2,beta\n", encoding="utf-8")

    schema, table = os.environ.get("TEST_SCHEMA", "reference_raw"), "pytest_widgets"
    rows = db.load_csv_file(analytics, schema, table, csv_path)
    analytics.commit()
    try:
        assert rows == 2
        got = analytics.execute(f"SELECT id, name FROM {schema}.{table} ORDER BY id").fetchall()
        assert got == [("1", "alpha"), ("2", "beta")]
    finally:
        analytics.execute(f"DROP TABLE IF EXISTS {schema}.{table}")
        analytics.commit()
