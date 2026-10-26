"""Approval files written by record_approval.py are the ones the breaking-change gate reads."""

import argparse

import pytest
import record_approval as ra
import yaml
from dgp_governance import approvals


def test_major_version_approval(tmp_repo):
    f = approvals.record("customers", 2, "sales", user="jane", pr="42")
    assert f.name == "customers-v2.yaml"
    assert yaml.safe_load(f.read_text(encoding="utf-8"))["version"] == 2
    assert approvals.approved_teams("customers", 2) == {"sales"}


def test_retirement_approval(tmp_repo):
    f = approvals.record("customers", "retire", "sales")
    assert f.name == "customers-retire.yaml"
    assert yaml.safe_load(f.read_text(encoding="utf-8"))["change"] == "retire"
    assert approvals.approved_teams("customers", "retire") == {"sales"}


def test_a_team_is_recorded_once(tmp_repo):
    assert approvals.record("customers", "retire", "sales") is not None
    assert approvals.record("customers", "retire", "sales") is None
    doc = yaml.safe_load(
        (tmp_repo / "governance/approvals/customers-retire.yaml").read_text(encoding="utf-8")
    )
    assert [a["team"] for a in doc["approved_by"]] == ["sales"]


def test_cli_accepts_retire_and_rejects_other_words():
    assert ra._version("retire") == "retire"
    assert ra._version("3") == 3
    with pytest.raises(argparse.ArgumentTypeError, match="retire"):
        ra._version("latest")
