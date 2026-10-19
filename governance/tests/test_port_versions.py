"""Versioned output-port lifecycle (ADR-0019 §3), on throwaway repos."""

import sys
from datetime import date
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "policies"))
import check_port_versions as pv  # noqa: E402

TODAY = date(2026, 10, 19)


def _port(
    root: Path,
    name: str,
    major: int = 1,
    status: str = "active",
    sunset: str | None = None,
    version_of: str | None = None,
    inputs: list[str] | None = None,
    domain: str = "customer",
):
    contracts = root / "domains" / domain / "contracts"
    products = root / "domains" / domain / "data_products"
    contracts.mkdir(parents=True, exist_ok=True)
    products.mkdir(parents=True, exist_ok=True)
    doc = {"version": f"{major}.0.0", "status": status,
           "schema": [{"name": name, "tags": ["output_port"]}]}  # fmt: skip
    if sunset:
        doc["customProperties"] = [{"property": "sunsetDate", "value": sunset}]
    (contracts / f"{name}.odcs.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    defn = {"name": name, "inputs": inputs or []}
    if version_of:
        defn["version_of"] = version_of
    (products / f"{name}.yaml").write_text(yaml.safe_dump(defn), encoding="utf-8")


def _consumer(root: Path, reads: str):
    _port(root, "sales_performance", domain="sales", inputs=[reads])


def test_unversioned_repo_is_clean(tmp_path):
    _port(tmp_path, "customers")
    assert pv.violations(tmp_path, TODAY) == ([], [])


def test_a_release_must_deprecate_the_old_version_with_a_sunset(tmp_path):
    _port(tmp_path, "customers")
    _port(tmp_path, "customers_v2", major=2, version_of="customers")
    errs, _ = pv.violations(tmp_path, TODAY)
    assert any("has a successor (customers_v2)" in e for e in errs)


def test_within_the_window_consumers_are_a_notice(tmp_path):
    _port(tmp_path, "customers", status="deprecated", sunset="2026-12-31")
    _port(tmp_path, "customers_v2", major=2, version_of="customers")
    _consumer(tmp_path, "customer.customers")
    errs, notes = pv.violations(tmp_path, TODAY)
    assert errs == []
    assert "73 days" in notes[0] and "still read by sales.sales_performance" in notes[0]


def test_after_the_sunset_ci_fails_and_names_who_must_migrate(tmp_path):
    _port(tmp_path, "customers", status="deprecated", sunset="2026-10-01")
    _port(tmp_path, "customers_v2", major=2, version_of="customers")
    _consumer(tmp_path, "customer.customers")
    errs, _ = pv.violations(tmp_path, TODAY)
    assert errs == ["customers: sunset 2026-10-01 has passed — retire it after migrating "
                    "sales.sales_performance"]  # fmt: skip


def test_versions_are_named_numbered_and_linked(tmp_path):
    _port(tmp_path, "customers", status="deprecated", sunset="2026-12-31")
    _port(tmp_path, "customers_v2", major=3, version_of="customers")
    _port(tmp_path, "orders")
    _port(tmp_path, "orders_v2", major=2)  # forgot version_of
    errs, _ = pv.violations(tmp_path, TODAY)
    assert any("must carry major 2" in e for e in errs)
    assert any("declare `version_of: orders`" in e for e in errs)


def test_deprecated_needs_a_sunset_and_a_successor(tmp_path):
    _port(tmp_path, "customers", status="deprecated")
    errs, _ = pv.violations(tmp_path, TODAY)
    assert any("without a `sunsetDate`" in e for e in errs)
    assert any("no successor" in e for e in errs)
