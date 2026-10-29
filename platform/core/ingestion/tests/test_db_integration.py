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


SYSTEM = {"_system_batch_id": "text", "_system_source_path": "text"}


def test_system_columns_are_added_and_stamped_per_file(tmp_path: Path, analytics):
    """The platform's columns follow the file's, and every row of a file gets that file's values,
    in the one COPY. No default is left behind."""
    schema, table = os.environ.get("TEST_SCHEMA", "reference_raw"), "pytest_system"
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    a.write_text("id\n1\n2\n", encoding="utf-8")
    b.write_text("id\n3\n", encoding="utf-8")
    try:
        stamp_a = {"_system_batch_id": "b1", "_system_source_path": "s3://x/a.csv"}
        stamp_b = {"_system_batch_id": "b1", "_system_source_path": "s3://x/b.csv"}
        db.load_csv_file(analytics, schema, table, a, system=SYSTEM, stamp=stamp_a)
        db.load_csv_file(analytics, schema, table, b, append=True, system=SYSTEM, stamp=stamp_b)
        analytics.commit()
        rows = analytics.execute(
            f"SELECT id, _system_batch_id, _system_source_path FROM {schema}.{table} ORDER BY id"
        ).fetchall()
        assert rows == [
            ("1", "b1", "s3://x/a.csv"),
            ("2", "b1", "s3://x/a.csv"),
            ("3", "b1", "s3://x/b.csv"),
        ]
        defaults = analytics.execute(
            "SELECT count(*) FROM information_schema.columns WHERE table_schema = %s "
            "AND table_name = %s AND column_default IS NOT NULL",
            (schema, table),
        ).fetchone()
        assert defaults == (0,)
    finally:
        analytics.execute(f"DROP TABLE IF EXISTS {schema}.{table}")
        analytics.commit()


def test_a_table_from_before_gets_the_system_columns_in_place(tmp_path: Path, analytics):
    """A raw table loaded before system columns existed gains them on its next load without being
    dropped, so the views built on it survive."""
    schema, table = os.environ.get("TEST_SCHEMA", "reference_raw"), "pytest_upgrade"
    csv_path = tmp_path / "u.csv"
    csv_path.write_text("id\n1\n", encoding="utf-8")
    view = f"{schema}.{table}_v"
    try:
        db.load_csv_file(analytics, schema, table, csv_path)  # the old shape: data columns only
        analytics.execute(f"CREATE VIEW {view} AS SELECT id FROM {schema}.{table}")
        analytics.commit()
        stamp = {"_system_batch_id": "b2", "_system_source_path": "s3://x/u.csv"}
        db.load_csv_file(analytics, schema, table, csv_path, system=SYSTEM, stamp=stamp)
        analytics.commit()
        assert analytics.execute(f"SELECT id FROM {view}").fetchall() == [("1",)]
        assert db.columns_of(analytics, schema, table) == {"id", *SYSTEM}
    finally:
        analytics.execute(f"DROP VIEW IF EXISTS {view}")
        analytics.execute(f"DROP TABLE IF EXISTS {schema}.{table}")
        analytics.commit()


def test_a_byte_order_mark_is_not_part_of_the_first_column(tmp_path: Path):
    csv_path = tmp_path / "bom.csv"
    csv_path.write_bytes("﻿product_category_name,english\nbeleza_saude,health_beauty\n".encode())
    assert db.csv_header(csv_path) == ["product_category_name", "english"]
