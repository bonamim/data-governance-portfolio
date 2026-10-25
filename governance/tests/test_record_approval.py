"""Approval files written by record_approval.py are the ones the breaking-change gate reads."""

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "policies"))
import check_breaking_changes as gate  # noqa: E402
import record_approval as ra  # noqa: E402


def test_major_version_approval(tmp_path, monkeypatch):
    f = ra.record("customers", 2, "sales", user="jane", pr="42", root=tmp_path)
    assert f.name == "customers-v2.yaml"
    assert yaml.safe_load(f.read_text())["version"] == 2
    monkeypatch.setattr(gate, "REPO_ROOT", tmp_path)
    assert gate._approved_teams("customers", 2) == {"sales"}


def test_retirement_approval(tmp_path, monkeypatch):
    f = ra.record("customers", "retire", "sales", root=tmp_path)
    assert f.name == "customers-retire.yaml"
    assert yaml.safe_load(f.read_text())["change"] == "retire"
    monkeypatch.setattr(gate, "REPO_ROOT", tmp_path)
    assert gate._approved_teams("customers", "retire") == {"sales"}


def test_a_team_is_recorded_once(tmp_path):
    assert ra.record("customers", "retire", "sales", root=tmp_path) is not None
    assert ra.record("customers", "retire", "sales", root=tmp_path) is None
    doc = yaml.safe_load((tmp_path / "governance/approvals/customers-retire.yaml").read_text())
    assert [a["team"] for a in doc["approved_by"]] == ["sales"]


def test_cli_accepts_retire_and_rejects_other_words():
    assert ra._version("retire") == "retire"
    assert ra._version("3") == 3
    try:
        ra._version("latest")
    except Exception as e:
        assert "retire" in str(e)
    else:
        raise AssertionError("'latest' should be rejected")
