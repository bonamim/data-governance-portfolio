from datetime import datetime
from pathlib import Path

import pytest
from dgp_platform_ingestion.config import load_datasets, olist_files


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_load_datasets_discovers_every_domain(tmp_path: Path):
    _write(
        tmp_path,
        "domains/sales/ingestion/sources.yml",
        "domain: sales\n"
        "datasets:\n"
        "  - name: orders\n"
        "    source: {type: oltp, olist_file: olist_orders_dataset.csv, oltp_table: orders}\n"
        "    target: {raw_table: orders}\n",
    )
    _write(
        tmp_path,
        "domains/reference/ingestion/sources.yml",
        "domain: reference\n"
        "datasets:\n"
        "  - name: geolocation\n"
        "    source: {type: landing, olist_file: olist_geolocation_dataset.csv,\n"
        "             delivered_as: 'geo_{date:%Y%m%d}.csv'}\n"
        "    target: {raw_table: geolocation}\n",
    )

    datasets = load_datasets(tmp_path)
    by_name = {d.name: d for d in datasets}

    assert set(by_name) == {"orders", "geolocation"}
    assert by_name["orders"].raw_schema == "sales_raw"
    assert by_name["orders"].oltp_table == "orders"
    assert by_name["geolocation"].raw_schema == "reference_raw"
    # The (simulated) partner's own file name for a business date (ADR-0024).
    assert by_name["geolocation"].source_system == "olist"
    assert by_name["geolocation"].delivered_name(datetime(2026, 10, 12)) == "geo_20261012.csv"
    assert by_name["orders"].delivered_name(datetime(2026, 10, 12)) == "olist_orders_dataset.csv"
    assert olist_files(datasets) == {
        "olist_orders_dataset.csv",
        "olist_geolocation_dataset.csv",
    }


def test_oltp_dataset_requires_oltp_table(tmp_path: Path):
    _write(
        tmp_path,
        "domains/sales/ingestion/sources.yml",
        "domain: sales\n"
        "datasets:\n"
        "  - name: orders\n"
        "    source: {type: oltp, olist_file: olist_orders_dataset.csv}\n"
        "    target: {raw_table: orders}\n",
    )
    with pytest.raises(ValueError, match="oltp_table"):
        load_datasets(tmp_path)


def test_empty_repo_raises(tmp_path: Path):
    with pytest.raises(RuntimeError, match="no source config"):
        load_datasets(tmp_path)
