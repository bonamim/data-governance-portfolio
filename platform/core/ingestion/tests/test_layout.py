"""Landing layout rules (ADR-0024): partitions derived from the contract, keys, retention."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from dgp_platform_ingestion import layout
from dgp_platform_ingestion.layout import FilenameMismatch, LandingRules
from dgp_platform_quality.rules import Contract, load_contract

REPO = Path(__file__).resolve().parents[4]
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
        "domains/reference/contracts/landing/geolocation.odcs.yaml",
    ],
)
def test_repo_landing_contracts_are_executable(contract):
    assert LandingRules.from_contract(load_contract(REPO / contract)).problems() == []


class _Obj:
    def __init__(self, filename: str, minutes_ago: int = 60):
        self.filename = filename
        self.last_modified = ARRIVED - timedelta(minutes=minutes_ago)


def _names(batches) -> list[tuple]:
    return [(b.partition, sorted(f.filename for f in b.files), bool(b.marker)) for b in batches]


def test_expected_files_waits_for_the_whole_partition():
    rules = _rules(completionSignal="expected_files", expectedFiles=2,
                   filenamePattern=r"^part(?P<n>\d)_(?P<date>\d{8})\.csv$")  # fmt: skip
    objs = [_Obj("part1_20261014.csv"), _Obj("part2_20261014.csv"), _Obj("part1_20261015.csv"),
            _Obj("garbage.csv")]  # fmt: skip
    ready, waiting = layout.plan_batches(rules, objs, ARRIVED)
    assert _names(ready) == [
        (None, ["garbage.csv"], False),  # unparseable: its own batch, quarantined by naming
        ("dt=2026-10-14", ["part1_20261014.csv", "part2_20261014.csv"], False),
    ]
    assert _names(waiting) == [("dt=2026-10-15", ["part1_20261015.csv"], False)]


def test_marker_closes_its_partition_only():
    rules = _rules(completionSignal="marker", completionMarker=r"^_DONE_(?P<date>\d{8})$",
                   filenamePattern=r"^tx_(?P<date>\d{8})_\d+\.csv$")  # fmt: skip
    objs = [_Obj("tx_20261014_1.csv"), _Obj("tx_20261014_2.csv"), _Obj("_DONE_20261014"),
            _Obj("tx_20261015_1.csv"), _Obj("_DONE_20261016")]  # fmt: skip
    ready, waiting = layout.plan_batches(rules, objs, ARRIVED)
    assert _names(ready) == [("dt=2026-10-14", ["tx_20261014_1.csv", "tx_20261014_2.csv"], True)]
    # a marker with no data yet is still waiting (incomplete), as is data with no marker
    assert _names(waiting) == [("dt=2026-10-15", ["tx_20261015_1.csv"], False),
                               ("dt=2026-10-16", [], True)]  # fmt: skip


def test_per_file_and_settle_batches_are_single_files():
    rules = _rules(completionSignal="settle", settleMinutes=30)
    ready, waiting = layout.plan_batches(rules, [_Obj("a.csv", 60), _Obj("b.csv", 5)], ARRIVED)
    assert _names(ready) == [(None, ["a.csv"], False)]
    assert _names(waiting) == [(None, ["b.csv"], False)]


def test_batch_signal_rules_are_validated():
    assert "needs expectedFiles" in " ".join(_rules(completionSignal="expected_files").problems())
    assert "needs a completionMarker" in " ".join(_rules(completionSignal="marker").problems())
    bad = _rules(completionSignal="marker", completionMarker=r"^_SUCCESS$",
                 filenamePattern=r"^x_(?P<date>\d{8})\.csv$")  # fmt: skip
    assert "completionMarker needs the partition's" in " ".join(bad.problems())


def test_deadline_owes_the_previous_business_day():
    rules = LandingRules(time_of_availability=layout._parse_toa("04:00+00:00"))
    due, deadline = rules.due_partition(datetime(2026, 10, 16, 9, 0, tzinfo=UTC))
    assert (due, deadline) == ("dt=2026-10-15", datetime(2026, 10, 16, 4, 0, tzinfo=UTC))
    # before today's deadline, yesterday's deadline (for dt=D-2) is the one in force
    due, _ = rules.due_partition(datetime(2026, 10, 16, 3, 0, tzinfo=UTC))
    assert due == "dt=2026-10-14"
    assert LandingRules().due_partition(ARRIVED) is None
    assert layout._parse_toa("06:30+02:00").hour == 4


def test_repo_contract_reads_its_delivery_deadline():
    path = REPO / "domains/customer_experience/contracts/landing/reviews.odcs.yaml"
    rules = LandingRules.from_contract(load_contract(path))
    assert rules.time_of_availability is not None and rules.time_of_availability.hour == 4
    assert rules.business_date_lag_days == 1


def test_a_business_date_from_the_future_is_flagged():
    rules = _rules(filenamePattern=r"^r_(?P<date>\d{8})\.csv$")
    now = datetime(2026, 10, 16, 12, tzinfo=UTC)
    assert not rules.in_future("dt=2026-10-17", now)  # tomorrow: a partner a timezone ahead
    assert rules.in_future("dt=2026-10-18", now)
    assert rules.in_future("dt=2200-04-30", now)
    assert not _rules().in_future("dt=2200-04-30", now)  # arrival-time partitions can't be
