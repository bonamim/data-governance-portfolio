from pathlib import Path

from dgp_platform_quality.contract import ColumnRule, Contract
from dgp_platform_quality.engine import validate_file

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
