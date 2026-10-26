"""Per-model write-audit-publish planning (ADR-0015): windows, order, what is built when."""

from datetime import datetime
from pathlib import Path

import pytest
from dgp_platform_orchestration import wap

REPO = Path(__file__).resolve().parents[4]


def _mb(batch_size="month", lookback=1) -> wap.WapModel:
    return wap.WapModel(
        "orders",
        "sales_silver",
        "orders",
        "incremental",
        "microbatch",
        "order_purchase_timestamp",
        batch_size,
        lookback,
    )


def _manifest(edges: dict[str, list[str]], project="dgp_sales") -> dict:
    """A tiny manifest: model name -> its children."""
    names = set(edges) | {c for cs in edges.values() for c in cs}
    nodes = {
        f"model.{project}.{n}": {
            "resource_type": "model",
            "package_name": project,
            "name": n,
            "schema": "s",
            "alias": n,
            "config": {"materialized": "table"},
        }
        for n in names
    }
    child = {
        f"model.{project}.{n}": [f"model.{project}.{c}" for c in edges.get(n, [])] for n in names
    }
    parent = {u: [p for p, cs in child.items() if u in cs] for u in child}
    return {"nodes": nodes, "child_map": child, "parent_map": parent}


def test_normal_run_takes_current_batch_plus_lookback():
    w = wap.batch_window(_mb(lookback=1), now=datetime(2026, 10, 16, 13, 5))
    assert (w.start, w.end) == (datetime(2026, 9, 1), datetime(2026, 11, 1))


def test_backfill_window_is_aligned_to_whole_batches():
    w = wap.batch_window(_mb(), datetime(2026, 10, 16), datetime(2017, 1, 15), datetime(2017, 3, 1))
    assert (w.start, w.end) == (datetime(2017, 1, 1), datetime(2017, 3, 1))  # end on a boundary
    w = wap.batch_window(_mb(), datetime(2026, 10, 16), datetime(2017, 1, 1), datetime(2017, 2, 10))
    assert w.end == datetime(2017, 3, 1)  # a partial batch is included whole
    assert w.flags() == [
        "--event-time-start",
        "2017-01-01T00:00:00",
        "--event-time-end",
        "2017-03-01T00:00:00",
    ]


def test_daily_and_yearly_batches_and_empty_window():
    now = datetime(2026, 1, 1, 0, 30)
    assert wap.batch_window(_mb("day", 2), now).start == datetime(2025, 12, 30)
    assert wap.batch_window(_mb("year", 0), now).end == datetime(2027, 1, 1)
    with pytest.raises(ValueError, match="empty backfill window"):
        wap.batch_window(_mb(), now, datetime(2017, 3, 1), datetime(2017, 3, 1))


def test_wap_models_run_upstream_first_and_descendants_skip_quarantined():
    # brz -> orders (wap) -> perf (wap) -> dash;  orders -> report
    m = _manifest({"brz": ["orders"], "orders": ["perf", "report"], "perf": ["dash"]})
    names = ["perf", "orders"]
    assert [x.name for x in wap.wap_models(m, "sales", names)] == ["orders", "perf"]
    # WAP models are never in the plain set; dash and report follow their promotes.
    assert wap.plain_descendants(m, "sales", names, quarantined=[]) == ["dash", "report"]
    # perf quarantined: dash keeps reading perf's last good version, report still rebuilds.
    assert wap.plain_descendants(m, "sales", names, quarantined=["perf"]) == ["report"]
    assert wap.ancestors(m, "sales", "perf") == ["brz", "orders"]


def test_opt_in_comes_from_the_data_products():
    assert set(wap.wap_products(REPO, "sales")) == {"orders", "sales_performance"}
    assert wap.wap_products(REPO, "customer") == []
