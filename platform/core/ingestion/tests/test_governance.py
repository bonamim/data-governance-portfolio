"""The loader governs a raw table from its landing contract, the same way on every warehouse.

Unit tests: the statements are rendered, not run (test_db_integration.py runs the Postgres ones).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from dgp_platform_ingestion import governance
from dgp_platform_ingestion.governance import RawGovernance

REPO = Path(__file__).resolve().parents[4]
REVIEWS = REPO / "domains/customer_experience/contracts/landing/reviews.odcs.yaml"

GOV = RawGovernance(
    description="One row per widget.",
    columns={"id": "The widget's id.", "note": None},
    table_tags={"SNOWFLAKE.TAGS.SENSITIVITY": "RESTRICTED"},
    column_tags={"note": {"SNOWFLAKE.CORE.PRIVACY_CATEGORY": "IDENTIFIER"}},
)


def test_the_contract_resolves_with_inherited_meaning_and_tags():
    gov = RawGovernance.from_contract(REVIEWS)
    assert gov.description.startswith("One row per survey answer and order")
    # Inherited from the definition, not written on the landing column.
    assert gov.columns["review_comment_message"].startswith("The comment the customer wrote")
    assert gov.column_tags["review_comment_message"] == {
        "SNOWFLAKE.CORE.PRIVACY_CATEGORY": "IDENTIFIER",
        "SNOWFLAKE.TAGS.SENSITIVITY": "RESTRICTED",
    }
    # The table is as sensitive as its most sensitive column.
    assert gov.table_tags == {"SNOWFLAKE.TAGS.SENSITIVITY": "RESTRICTED"}


def test_postgres_gets_comments_only():
    assert governance.statements("postgres", "s_raw", "widgets", GOV) == [
        """COMMENT ON TABLE "s_raw"."widgets" IS 'One row per widget.'""",
        """COMMENT ON COLUMN "s_raw"."widgets"."id" IS 'The widget''s id.'""",
        """COMMENT ON COLUMN "s_raw"."widgets"."note" IS NULL""",
    ]


def test_snowflake_gets_comments_and_the_governed_tags():
    out = governance.statements("snowflake", "s_raw", "widgets", GOV)
    assert out[0] == "COMMENT ON TABLE s_raw.widgets IS 'One row per widget.'"
    assert out[-2:] == [
        "ALTER TABLE s_raw.widgets SET TAG SNOWFLAKE.TAGS.SENSITIVITY = 'RESTRICTED'",
        "ALTER TABLE s_raw.widgets MODIFY COLUMN note SET TAG "
        "SNOWFLAKE.CORE.PRIVACY_CATEGORY = 'IDENTIFIER'",
    ]


def test_only_the_columns_the_table_has():
    out = governance.statements("snowflake", "s_raw", "widgets", GOV, present={"id"})
    assert not any("note" in s for s in out)


def test_names_and_text_cannot_break_out_of_the_statement():
    gov = RawGovernance(description="a\\b 'c'", columns={'we"ird; drop': "x"})
    pg = governance.statements("postgres", "s", "t", gov)
    sf = governance.statements("snowflake", "s", "t", gov)
    assert pg[0].endswith("""IS 'a\\b ''c'''""")  # Postgres: a backslash is plain text
    assert sf[0].endswith("""IS 'a\\\\b ''c'''""")  # Snowflake: a backslash escapes
    assert '"we""ird; drop"' in pg[1] and '"we""ird; drop"' in sf[1]


def test_an_unknown_warehouse_is_refused():
    with pytest.raises(ValueError, match="bigquery"):
        governance.statements("bigquery", "s", "t", GOV)


def test_every_raw_table_carries_the_system_columns():
    names = ["_system_batch_id", "_system_source_path", "_system_partition", "_system_loaded_at"]
    landing = RawGovernance.from_contract(REVIEWS)
    assert list(landing.columns)[-4:] == names  # after the contract's own
    assert landing.columns["_system_batch_id"].startswith("The delivery batch")
    oltp = RawGovernance.from_contract(None)  # an OLTP source: no landing contract
    assert list(oltp.columns) == names and oltp.description is None
    assert oltp.column_tags["_system_loaded_at"] == {"SNOWFLAKE.TAGS.SENSITIVITY": "INTERNAL"}


def test_system_columns_have_a_type_on_each_warehouse():
    assert governance.system_columns("postgres")["_system_loaded_at"] == "timestamptz"
    assert governance.system_columns("snowflake")["_system_batch_id"] == "VARCHAR"


def test_an_oltp_table_that_no_longer_matches_its_contract_is_refused(tmp_path):
    from dgp_platform_ingestion import pipeline
    from dgp_platform_ingestion.config import load_datasets

    (orders,) = [d for d in load_datasets(REPO, "sales") if d.name == "orders"]
    dump = tmp_path / "orders.csv"
    dump.write_text("order_id,customer_id,status\n1,2,delivered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no longer matches its contract"):
        pipeline._check_oltp_schema(REPO, orders, dump)
