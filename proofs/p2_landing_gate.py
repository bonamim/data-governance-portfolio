#!/usr/bin/env python3
"""Proof P2 — landing quality gate.

A landing file that violates its ODCS contract is rejected by the quality engine — the gate that
quarantines it before anything is loaded to the warehouse. Self-contained: it runs the real engine
(`validate_file`) against the real `reviews` landing contract, on a clean file and a bad one.
"""

from __future__ import annotations

import csv
import sys
import tempfile
from pathlib import Path

from dgp_platform_quality.engine import validate_file
from dgp_platform_quality.rules import load_contract

REPO = Path(__file__).resolve().parents[1]
CONTRACT = REPO / "domains/customer_experience/contracts/landing/reviews.odcs.yaml"

HEADER = [
    "review_id",
    "order_id",
    "review_score",
    "review_comment_title",
    "review_comment_message",
    "review_creation_date",
    "review_answer_timestamp",
]
# Enough rows to clear the contract's completeness floor; the bad file injects a content violation.
N = 1001


def _rows(inject_bad: bool) -> list[list[str]]:
    rows = [[f"r{i}", f"o{i}", "5", "", "ok", "2026-01-01", ""] for i in range(N)]
    if inject_bad:
        # order_id missing (required) and review_score 99 (outside the 1-5 range)
        rows[0] = ["rX", "", "99", "", "", "2026-01-01", ""]
    return rows


def _write(rows: list[list[str]]) -> Path:
    fd, name = tempfile.mkstemp(suffix=".csv")
    with open(fd, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        w.writerows(rows)
    return Path(name)


def main() -> int:
    contract = load_contract(CONTRACT)
    good_file, bad_file = _write(_rows(False)), _write(_rows(True))
    try:
        ok = validate_file(good_file, contract)
        if not ok.passed:
            print(f"FAIL: a clean file was rejected — {ok.summary()}")
            return 1
        print(f"  OK — clean file passes: {ok.summary()}")

        bad = validate_file(bad_file, contract)
        if bad.passed:
            print("FAIL: a contract-violating file passed the gate")
            return 1
        reasons = "; ".join(a.detail for a in bad.failures())
        print(f"  OK — bad file rejected (would be quarantined, not loaded): {reasons}")
    finally:
        good_file.unlink(missing_ok=True)
        bad_file.unlink(missing_ok=True)

    print("\nP2 proven: a landing file that breaks its contract never reaches the warehouse.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
