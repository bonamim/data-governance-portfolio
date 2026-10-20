"""Derived semantic fields (e.g. a purchase month) may only be built from contract columns, never
from PII — check_semantics and check_ai_agents, on throwaway repos."""

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "policies"))
import check_ai_agents  # noqa: E402
import check_semantics  # noqa: E402

PII = next(iter(check_ai_agents._pii_values()))


def _repo(root: Path, field_expr: str, field_name: str = "order_purchase_month") -> None:
    sales = root / "domains" / "sales"
    for d in ("contracts", "data_products", "semantics"):
        (sales / d).mkdir(parents=True, exist_ok=True)
    contract = {
        "schema": [
            {
                "name": "orders",
                "tags": ["output_port"],
                "properties": [
                    {"name": "order_id", "classification": "internal"},
                    {"name": "order_purchase_timestamp", "classification": "public"},
                    {"name": "buyer_email", "classification": PII},
                ],
            }
        ]
    }
    (sales / "contracts" / "orders.odcs.yaml").write_text(yaml.safe_dump(contract))
    (sales / "data_products" / "orders.yaml").write_text(
        yaml.safe_dump({"name": "orders", "owner": {"team": "sales"}})
    )
    dialects = {"dialects": [{"dialect": "ANSI_SQL", "expression": field_expr}]}
    spec = {
        "name": "orders",
        "datasets": [
            {
                "name": "orders",
                "source": "analytics.sales_silver.orders",
                "description": "Orders.",
                "fields": [{"name": field_name, "description": "d", "expression": dialects}],
            }
        ],
        "metrics": [
            {
                "name": "order_count",
                "description": "d",
                "expression": {
                    "dialects": [
                        {"dialect": "ANSI_SQL", "expression": "COUNT(DISTINCT orders.order_id)"}
                    ]
                },
            }
        ],
    }
    (sales / "semantics" / "orders.semantic_model.yaml").write_text(yaml.safe_dump(spec))


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(check_semantics, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(check_ai_agents, "REPO_ROOT", tmp_path)
    return tmp_path


def test_derived_field_from_contract_columns_passes(repo):
    _repo(repo, "DATE_TRUNC('month', orders.order_purchase_timestamp)")
    assert check_semantics.main() == 0
    assert check_ai_agents.main() == 0


def test_derived_field_from_a_non_contract_column_is_blocked(repo, capsys):
    _repo(repo, "DATE_TRUNC('month', orders.shipped_at)")
    assert check_semantics.main() == 1
    assert "order_purchase_month" in capsys.readouterr().out


def test_unknown_field_without_contract_refs_is_blocked(repo):
    _repo(repo, "order_purchase_month")  # not a column, not derived from one
    assert check_semantics.main() == 1


def test_derived_field_wrapping_pii_is_blocked(repo, capsys):
    _repo(repo, "LOWER(orders.buyer_email)", field_name="email_domain")
    assert check_semantics.main() == 0  # a contract column, so the semantic check allows it…
    assert check_ai_agents.main() == 1  # …but no agent may expose PII, even derived
    assert "buyer_email" in capsys.readouterr().out
