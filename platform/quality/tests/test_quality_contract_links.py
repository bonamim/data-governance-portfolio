"""The landing gate executes the rules a column inherits from its definition."""

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
        )
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
        )
    )

    (rule,) = load_contract(path).columns

    assert rule.logical_type == "integer"
    assert (rule.minimum, rule.maximum) == (1, 5)
    assert rule.required is False
