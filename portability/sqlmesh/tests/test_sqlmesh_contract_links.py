"""SQLMesh's port schema and audits come from the resolved contract, inherited elements included."""

import importlib.util
from pathlib import Path

import yaml

# Loaded by path: platform/transform and portability/sqlmesh both have a sync_contracts.py.
_spec = importlib.util.spec_from_file_location(
    "sqlmesh_sync_contracts", Path(__file__).resolve().parents[1] / "sync_contracts.py"
)
sync_contracts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync_contracts)


def test_columns_and_audits_include_what_a_column_inherits(tmp_path):
    (tmp_path / "order_id.yaml").write_text(
        yaml.safe_dump({"physicalType": "text", "required": True, "unique": True})
    )
    contract = tmp_path / "orders.odcs.yaml"
    link = [{"type": "definition", "url": "order_id.yaml"}]
    contract.write_text(
        yaml.safe_dump(
            {
                "schema": [
                    {
                        "name": "orders",
                        "properties": [
                            {"name": "order_id", "authoritativeDefinitions": link},
                            {"name": "order_status", "physicalType": "text"},
                        ],
                    }
                ]
            }
        )
    )

    assert sync_contracts.columns(contract) == ["order_id TEXT", "order_status TEXT"]
    audits = sync_contracts.audits(contract, incremental=False)
    assert "not_null(columns := (order_id))" in audits
    assert "unique_values(columns := (order_id))" in audits
