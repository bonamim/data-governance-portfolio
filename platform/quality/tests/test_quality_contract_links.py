"""The landing gate executes the rules a column inherits from its definition."""

import pytest
import yaml
from dgp_platform_quality.rules import load_contract


def test_landing_rules_include_what_a_column_inherits(tmp_path):
    (tmp_path / "review_score.yaml").write_text(
        yaml.safe_dump(
            {
                "logicalType": "integer",
                "required": True,
                "logicalTypeOptions": {"minimum": 1, "maximum": 5},
            }
        ),
        encoding="utf-8",
    )
    path = tmp_path / "reviews.odcs.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "id": "reviews",
                "schema": [
                    {
                        "name": "reviews",
                        "properties": [
                            {
                                "name": "review_score",
                                "required": False,  # the contract's own value wins
                                "authoritativeDefinitions": [
                                    {"type": "definition", "url": "review_score.yaml"}
                                ],
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    (rule,) = load_contract(path).columns

    assert rule.logical_type == "integer"
    assert (rule.minimum, rule.maximum) == (1, 5)
    assert rule.required is False


def _contract_with_quality(tmp_path, quality):
    path = tmp_path / "reviews.odcs.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "id": "reviews",
                "schema": [
                    {
                        "name": "reviews",
                        "properties": [{"name": "review_id"}, {"name": "order_id"}],
                        "quality": quality,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_duplicate_values_on_several_columns_is_a_unique_key(tmp_path):
    rule = {
        "type": "library",
        "metric": "duplicateValues",
        "arguments": {"properties": ["review_id", "order_id"]},
        "mustBe": 0,
    }
    contract = load_contract(_contract_with_quality(tmp_path, [rule]))
    assert contract.unique_keys == [("review_id", "order_id")]


def test_a_duplicate_values_rule_the_gate_cant_run_is_an_error(tmp_path):
    rule = {
        "type": "library",
        "metric": "duplicateValues",
        "arguments": {"properties": ["review_id", "order_id"]},
        "mustBeLessThan": 10,
    }
    with pytest.raises(ValueError, match="mustBe: 0"):
        load_contract(_contract_with_quality(tmp_path, [rule]))
