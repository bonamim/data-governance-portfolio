"""P20 tests each engine against the resolved contract (its temp copy cannot resolve links)."""

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import p20_engine_portability as p20  # noqa: E402


def test_engine_contract_is_resolved_and_points_at_the_engine(tmp_path, monkeypatch):
    (tmp_path / "governance").mkdir()
    (tmp_path / "governance" / "order_id.yaml").write_text(
        yaml.safe_dump({"description": "Order key.", "required": True})
    )
    contracts = tmp_path / "domains" / "sales" / "contracts"
    contracts.mkdir(parents=True)
    link = [{"type": "definition", "url": "../../../governance/order_id.yaml"}]
    (contracts / "orders.odcs.yaml").write_text(
        yaml.safe_dump(
            {
                "schema": [
                    {
                        "name": "orders",
                        "properties": [{"name": "order_id", "authoritativeDefinitions": link}],
                    }
                ]
            }
        )
    )
    monkeypatch.setattr(p20, "REPO", tmp_path)

    doc = p20.engine_contract("sales/contracts/orders", "sqlmesh_sales_silver", "sqlmesh")

    (prop,) = doc["schema"][0]["properties"]
    assert prop["description"] == "Order key." and prop["required"] is True
    (server,) = doc["servers"]
    assert server["server"] == "sqlmesh" and server["schema"] == "sqlmesh_sales_silver"
