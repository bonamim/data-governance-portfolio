from pathlib import Path

from dgp_platform_quality.engine import validate_file
from dgp_platform_quality.rules import ColumnRule, Contract

CONTRACT = Contract(
    id="reviews",
    name="Reviews",
    version="1.0.0",
    path=Path("reviews.odcs.yaml"),
    columns=[
        ColumnRule(name="review_id", logical_type="string", required=True, unique=True),
        ColumnRule(
            name="review_score", logical_type="integer", required=True, minimum=1, maximum=5
        ),
    ],
)


def _write(tmp_path: Path, rows: str) -> Path:
    p = tmp_path / "reviews.csv"
    p.write_text(rows, encoding="utf-8")
    return p


def test_valid_file_passes(tmp_path: Path):
    csv = _write(tmp_path, "review_id,review_score\nr1,5\nr2,4\n")
    result = validate_file(csv, CONTRACT)
    assert result.passed, result.summary()
    assert result.rows == 2


def test_score_out_of_range_fails(tmp_path: Path):
    csv = _write(tmp_path, "review_id,review_score\nr1,5\nr2,9\n")
    result = validate_file(csv, CONTRACT)
    assert not result.passed
    assert any(a.check == "range" and a.column == "review_score" for a in result.failures())


def test_duplicate_id_fails(tmp_path: Path):
    csv = _write(tmp_path, "review_id,review_score\nr1,5\nr1,4\n")
    result = validate_file(csv, CONTRACT)
    assert not result.passed
    assert any(a.check == "unique" and a.column == "review_id" for a in result.failures())


def test_missing_required_value_fails(tmp_path: Path):
    csv = _write(tmp_path, "review_id,review_score\nr1,5\n,4\n")
    result = validate_file(csv, CONTRACT)
    assert not result.passed
    assert any(a.check == "not_null" and a.column == "review_id" for a in result.failures())


def test_missing_column_fails(tmp_path: Path):
    csv = _write(tmp_path, "review_id\nr1\nr2\n")
    result = validate_file(csv, CONTRACT)
    assert not result.passed
    assert any(a.check == "schema.columns_present" for a in result.failures())


def test_completeness_floor_fails_when_too_few_rows(tmp_path: Path):
    contract = Contract(
        id="reviews",
        name="Reviews",
        version="1.0.0",
        path=Path("reviews.odcs.yaml"),
        columns=[ColumnRule(name="review_id", required=True)],
        min_rows=5,
    )
    csv = _write(tmp_path, "review_id\nr1\nr2\n")  # 2 rows, floor is 5
    result = validate_file(csv, contract)
    assert not result.passed
    assert any(a.check == "completeness.row_count" for a in result.failures())


PAIR = Contract(
    id="reviews",
    name="Reviews",
    version="1.0.0",
    path=Path("reviews.odcs.yaml"),
    columns=[ColumnRule(name="review_id", required=True), ColumnRule(name="order_id")],
    unique_keys=[("review_id", "order_id")],
)


def test_unique_key_allows_each_column_to_repeat(tmp_path: Path):
    # One survey answer on two orders, and one order with two answers: the pair is still unique.
    csv = _write(tmp_path, "review_id,order_id\nr1,o1\nr1,o2\nr2,o1\n")
    result = validate_file(csv, PAIR)
    assert result.passed, result.summary()


def test_unique_key_fails_on_a_repeated_pair(tmp_path: Path):
    csv = _write(tmp_path, "review_id,order_id\nr1,o1\nr1,o2\nr1,o1\n")
    result = validate_file(csv, PAIR)
    assert not result.passed
    [failure] = result.failures()
    assert (failure.check, failure.column) == ("unique_key", "review_id+order_id")
    assert failure.detail == "1 duplicate combinations"


def test_unique_key_leaves_empty_values_to_not_null(tmp_path: Path):
    csv = _write(tmp_path, "review_id,order_id\nr1,\nr1,\n")
    result = validate_file(csv, PAIR)
    assert [a.check for a in result.failures()] == []
