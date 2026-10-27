"""dbt test results land on the assertions the dbt recipe registers (ADR-0018)."""

import pytest
from dgp_platform_catalog.publish_dbt_results import dbt_test_results

META = {"generated_at": "2026-10-27T14:00:00Z", "invocation_id": "inv-1"}


def _manifest(depends_on: list[str]) -> dict:
    return {
        "nodes": {
            "model.dgp_sales.orders": {
                "resource_type": "model",
                "name": "orders",
                "database": "analytics",
                "schema": "sales_silver",
                "alias": "orders",
            },
            "model.dgp_sales.payments": {
                "resource_type": "model",
                "name": "payments",
                "database": "analytics",
                "schema": "sales_silver",
                "alias": "order_payments",
            },
            "test.dgp_sales.not_null_orders_order_id": {
                "resource_type": "test",
                "depends_on": {"nodes": depends_on},
            },
        },
        "sources": {},
    }


def _results(status: str, **extra) -> dict:
    timing = [{"name": "execute", "started_at": "2026-10-27T15:00:00Z"}]
    return {
        "metadata": META,
        "results": [
            {"unique_id": "model.dgp_sales.orders", "status": "success", "timing": timing},
            {
                "unique_id": "test.dgp_sales.not_null_orders_order_id",
                "status": status,
                "timing": timing,
                **extra,
            },
        ],
    }


def test_a_test_result_resolves_to_the_tested_warehouse_dataset():
    [r] = dbt_test_results(_results("pass"), _manifest(["model.dgp_sales.orders"]))
    assert r.unique_id == "test.dgp_sales.not_null_orders_order_id"
    assert r.dataset == "analytics.sales_silver.orders"
    assert (r.status, r.invocation_id, r.native) == ("pass", "inv-1", {})
    assert r.timestamp_ms == 1793113200000  # the execute start, not generated_at
    assert not r.multi_upstream


def test_a_failure_carries_dbt_message_and_failing_rows():
    results = _results("fail", message="Got 3 results", failures=3)
    [r] = dbt_test_results(results, _manifest(["model.dgp_sales.orders"]))
    assert r.native == {"message": "Got 3 results", "failures": "3"}


def test_an_audit_marks_its_results():
    [r] = dbt_test_results(_results("pass"), _manifest(["model.dgp_sales.orders"]), "audit")
    assert r.native == {"stage": "audit"}


def test_a_multi_upstream_test_gets_one_result_per_dataset():
    deps = ["model.dgp_sales.orders", "model.dgp_sales.payments"]
    runs = dbt_test_results(_results("pass"), _manifest(deps))
    assert [r.dataset for r in runs] == [
        "analytics.sales_silver.orders",
        "analytics.sales_silver.order_payments",
    ]
    assert all(r.multi_upstream for r in runs)


def test_a_run_without_tests_has_no_results():
    results = _results("pass")
    results["results"] = results["results"][:1]
    assert dbt_test_results(results, _manifest(["model.dgp_sales.orders"])) == []


def test_the_assertion_urn_is_the_dbt_sources():
    """Same guid as DataHub's dbt source, or the events would land on assertions nobody sees."""
    pytest.importorskip("datahub")
    from datahub.emitter.mce_builder import datahub_guid, make_assertion_urn
    from dgp_platform_catalog.publish_dbt_results import _mcps

    runs = dbt_test_results(_results("fail"), _manifest(["model.dgp_sales.orders"]))
    [mcp] = _mcps(runs)
    uid = "test.dgp_sales.not_null_orders_order_id"
    assert mcp.entityUrn == make_assertion_urn(datahub_guid({"platform": "dbt", "name": uid}))
    assert mcp.aspect.asserteeUrn == (
        "urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.sales_silver.orders,PROD)"
    )
    assert (mcp.aspect.result.type, mcp.aspect.result.severity) == ("FAILURE", "HIGH")
