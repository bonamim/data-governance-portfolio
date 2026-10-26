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
    except (RuntimeError, psycopg.Error) as exc:  # pragma: no cover - needs a running warehouse
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


def test_reload_keeps_dependent_views(tmp_path: Path, analytics):
    """A reload with the same columns must not drop what is built on the table (output-port views,
    masking views, grants) — only a schema change may."""
    schema, table = os.environ.get("TEST_SCHEMA", "reference_raw"), "pytest_reload"
    first, second = tmp_path / "a.csv", tmp_path / "b.csv"
    first.write_text("id,name\n1,alpha\n", encoding="utf-8")
    second.write_text("id,name\n2,beta\n3,gamma\n", encoding="utf-8")
    view = f"{schema}.{table}_v"
    try:
        db.load_csv_file(analytics, schema, table, first)
        analytics.execute(f"CREATE VIEW {view} AS SELECT * FROM {schema}.{table}")
        analytics.commit()

        assert db.load_csv_file(analytics, schema, table, second) == 2
        analytics.commit()
        assert analytics.execute(f"SELECT count(*) FROM {view}").fetchone()[0] == 2  # view survived

        changed = tmp_path / "c.csv"
        changed.write_text("id,name,extra\n4,delta,x\n", encoding="utf-8")
        db.load_csv_file(analytics, schema, table, changed)
        analytics.commit()
        assert analytics.execute("SELECT to_regclass(%s)", (view,)).fetchone()[0] is None
    finally:
        analytics.execute(f"DROP TABLE IF EXISTS {schema}.{table} CASCADE")
        analytics.commit()
