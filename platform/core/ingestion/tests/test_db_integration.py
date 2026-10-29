"""Integration test for the generic CSV loader against the analytics warehouse.

Skips automatically when the warehouse is not reachable (e.g. `make up` not run),
so the unit suite stays runnable anywhere.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from dgp_platform_ingestion import db, governance  # noqa: E402
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
    masking views, grants). Only a schema change may."""
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


def test_governance_documents_the_table_and_its_columns(tmp_path: Path, analytics):
    """The raw table carries its landing contract's descriptions. A column the file doesn't have is
    skipped, not an error."""
    schema, table = os.environ.get("TEST_SCHEMA", "reference_raw"), "pytest_comments"
    csv_path = tmp_path / "c.csv"
    csv_path.write_text("id,name\n1,alpha\n", encoding="utf-8")
    gov = governance.RawGovernance("One row per widget.", {"id": "The widget id.", "gone": "x"})
    try:
        db.load_csv_file(analytics, schema, table, csv_path)
        present = db.columns_of(analytics, schema, table)
        db.execute_all(analytics, governance.statements("postgres", schema, table, gov, present))
        analytics.commit()
        rel = f"'{schema}.{table}'::regclass"
        assert analytics.execute(f"SELECT obj_description({rel})").fetchone() == (
            "One row per widget.",
        )
        assert analytics.execute(f"SELECT col_description({rel}, 1)").fetchone() == (
            "The widget id.",
        )
    finally:
        analytics.execute(f"DROP TABLE IF EXISTS {schema}.{table}")
        analytics.commit()


def test_governance_runs_on_the_empty_table_before_the_first_row(tmp_path: Path, analytics):
    """The table is governed as soon as it exists: no row is ever in an undescribed table."""
    schema, table = os.environ.get("TEST_SCHEMA", "reference_raw"), "pytest_govern_first"
    csv_path = tmp_path / "g.csv"
    csv_path.write_text("id\n1\n2\n", encoding="utf-8")
    seen: list[int] = []

    def govern() -> list[str]:
        seen.append(analytics.execute(f"SELECT count(*) FROM {schema}.{table}").fetchone()[0])
        gov = governance.RawGovernance("One row per widget.", {"id": "The widget id."})
        return governance.statements("postgres", schema, table, gov)

    try:
        assert db.load_csv_file(analytics, schema, table, csv_path, governance=govern) == 2
        analytics.commit()
        assert seen == [0]
        rel = f"'{schema}.{table}'::regclass"
        assert analytics.execute(f"SELECT obj_description({rel})").fetchone() == (
            "One row per widget.",
        )
    finally:
        analytics.execute(f"DROP TABLE IF EXISTS {schema}.{table}")
        analytics.commit()
