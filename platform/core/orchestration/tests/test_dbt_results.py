"""dbt tasks hand their test results to the catalog, and a catalog outage never fails them."""

import json
import subprocess

from dgp_platform_orchestration import dbt_results


def _target(tmp_path, unique_ids):
    (tmp_path / "run_results.json").write_text(
        json.dumps({"results": [{"unique_id": u} for u in unique_ids]})
    )
    return tmp_path


def test_only_an_invocation_that_ran_tests_is_published(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    dbt_results.publish(_target(tmp_path, ["model.dgp_sales.orders"]))
    dbt_results.publish(tmp_path / "missing")
    assert calls == []


def test_cosmos_publishes_the_task_target_with_its_stage(tmp_path, monkeypatch):
    calls = []

    def run(cmd, **_):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "published", "")

    monkeypatch.setattr(subprocess, "run", run)
    target = tmp_path / "target"
    target.mkdir()
    _target(target, ["test.dgp_sales.not_null_orders_order_id"])
    dbt_results.cosmos_callback(str(tmp_path), stage="audit", context={})
    [cmd] = calls
    assert cmd[-4:] == [dbt_results.MODULE, str(target), "--stage", "audit"]


def test_an_unreachable_catalog_is_logged_not_raised(tmp_path, monkeypatch, caplog):
    def run(cmd, **_):
        return subprocess.CompletedProcess(cmd, 1, "", "ConnectionError: GMS down")

    monkeypatch.setattr(subprocess, "run", run)
    dbt_results.publish(_target(tmp_path, ["test.dgp_sales.x"]))
    assert "GMS down" in caplog.text
