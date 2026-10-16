"""Landing layout rules (ADR-0024): partitions derived from the contract, keys, retention."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from dgp_platform_ingestion import layout
from dgp_platform_ingestion.layout import FilenameMismatch, LandingRules
from dgp_platform_quality.contract import Contract, load_contract

REPO = Path(__file__).resolve().parents[3]
ARRIVED = datetime(2026, 10, 15, 21, 30, tzinfo=UTC)


def _rules(**props) -> LandingRules:
    return LandingRules.from_contract(Contract("c", "c", "1.0.0", Path("c"), properties=props))


def test_filename_partition_daily_compact_date():
    rules = _rules(filenamePattern=r"^reviews_(?P<date>\d{8})\.csv$")
    assert rules.partition_source == "filename"  # implied by a pattern
    assert rules.partition_for("reviews_20261014.csv", ARRIVED) == "dt=2026-10-14"


def test_filename_partition_hourly_and_monthly():
    hourly = _rules(
        partitionScheme="dt=YYYY-MM-DD/hr=HH",
        filenamePattern=r"^tx_(?P<date>\d{4}-\d{2}-\d{2})T(?P<hour>\d{2})\.csv$",
    )
    assert hourly.partition_for("tx_2026-10-14T07.csv", ARRIVED) == "dt=2026-10-14/hr=07"
    monthly = _rules(
        partitionScheme="dt=YYYY-MM", filenamePattern=r"^m_(?P<year>\d{4})(?P<month>\d{2})\.csv$"
    )
    assert monthly.partition_for("m_202609.csv", ARRIVED) == "dt=2026-09"


def test_arrival_partition_when_partner_names_carry_no_date():
    rules = _rules(partitionSource="arrival")
    assert rules.partition_for("anything.csv", ARRIVED) == "dt=2026-10-15"
    assert _rules(partitionScheme="dt=YYYY-MM-DD/hr=HH").partition_for("x", ARRIVED) == (
        "dt=2026-10-15/hr=21"
    )


def test_unexpected_filename_is_a_mismatch():
    rules = _rules(filenamePattern=r"^reviews_(?P<date>\d{8})\.csv$")
    with pytest.raises(FilenameMismatch):
        rules.partition_for("reviews_final_v2.csv", ARRIVED)
    assert rules.partition_from_arrival(ARRIVED) == "dt=2026-10-15"


def test_rule_problems_are_reported():
    assert _rules().problems() == []
    assert _rules(partitionScheme="dt=YYYY").problems()
    assert _rules(partitionSource="filename").problems()  # filename without a pattern
    assert _rules(retentionDays=100).problems()  # not a retention class
    assert _rules(completionSignal="marker").problems()  # designed, not built yet
    hourly_no_hour = _rules(
        partitionScheme="dt=YYYY-MM-DD/hr=HH", filenamePattern=r"^x_(?P<date>\d{8})\.csv$"
    )
    assert any("hour" in p for p in hourly_no_hour.problems())


def test_settle_waits_for_quiet_period():
    rules = _rules(completionSignal="settle", settleMinutes=10)
    assert not rules.is_settled(ARRIVED, ARRIVED + timedelta(minutes=5))
    assert rules.is_settled(ARRIVED, ARRIVED + timedelta(minutes=10))
    assert _rules().is_settled(ARRIVED, ARRIVED)  # per_file: immediately


def test_keys_and_retention():
    assert layout.inbox_prefix("olist", "reviews") == "olist/reviews/"
    assert (
        layout.accepted_key("olist", "reviews", "dt=2026-10-15", "f.csv")
        == "accepted/olist/reviews/dt=2026-10-15/f.csv"
    )
    key = layout.quarantine_key("olist", "reviews", "dt=2026-10-15", "manual__2026:01+00", "f.csv")
    assert key == "quarantine/olist/reviews/dt=2026-10-15/manual__2026_01_00/f.csv"
    rules = _rules(retentionDays=90)
    assert rules.retention_tag == "retention-class=90d"
    assert rules.retain_until(ARRIVED) == ARRIVED + timedelta(days=90)


def test_replay_takes_each_datasets_newest_file_as_of_a_date():
    pre = layout.accepted_prefix("olist", "sellers")
    keys = [pre + "dt=2026-10-14/a.csv", pre + "dt=2026-10-15/b.csv", pre + "dt=2026-10-15/c.csv"]
    # Nothing on the 16th: the state then is the newest file before it (last one in a partition).
    assert (
        layout.newest_as_of("olist", "sellers", keys, "2026-10-16") == pre + "dt=2026-10-15/c.csv"
    )
    assert (
        layout.newest_as_of("olist", "sellers", keys, "2026-10-14") == pre + "dt=2026-10-14/a.csv"
    )
    assert layout.newest_as_of("olist", "sellers", keys, "2026-10-13") is None
    hourly = [pre + "dt=2026-10-15/hr=23/x.csv", pre + "dt=2026-10-15/hr=07/y.csv"]
    assert layout.newest_as_of("olist", "sellers", hourly, "2026-10-15").endswith("hr=23/x.csv")
    assert layout.accepted_partition("olist", "sellers", hourly[0]) == "dt=2026-10-15/hr=23"


@pytest.mark.parametrize(
    "contract",
    [
        "domains/customer_experience/contracts/landing/reviews.odcs.yaml",
        "domains/marketplace/contracts/landing/sellers.odcs.yaml",
        "reference/contracts/landing/geolocation.odcs.yaml",
    ],
)
def test_repo_landing_contracts_are_executable(contract):
    assert LandingRules.from_contract(load_contract(REPO / contract)).problems() == []
