"""One discovery call is enough: owners, column classifications and governed metrics together."""

from dgp_ai.semantic import SemanticLayer


def _products() -> dict[str, dict]:
    return {p["data_product"]: p for p in SemanticLayer().list_data_products()}


def test_governed_product_lists_its_metrics_and_dimensions():
    sales = _products()["sales_performance"]

    assert sales["governed"] is True
    assert "total_revenue" in {m["name"] for m in sales["metrics"]}
    assert "customer_state" in {d["name"] for d in sales["dimensions"]}
    assert all(m["description"] for m in sales["metrics"])


def test_every_product_carries_its_column_classification():
    products = _products()

    assert products["customers"]["column_classification"]["customer_unique_id"] == "restricted"
    assert products["customers"]["column_privacy"]["customer_unique_id"] == "identifier"
    assert products["sales_performance"]["column_classification"]["total_revenue"] == "public"
    assert "metrics" not in products["customers"]  # ungoverned: classes yes, metrics no
