#!/usr/bin/env python3
"""Proof P6 — state and idempotency: reruns and backfills are safe (ADR-0006, ADR-0015 §4).

On the microbatch port ``sales.orders`` (monthly batches, write-audit-publish):

  1. normal run   processes only the latest batches (current month + lookback); for the static
                  Olist history that window is empty, so no published row is rewritten
  2. backfill     ``--event-time-start 2017-03-01 --event-time-end 2017-05-01`` rewrites exactly
                  the rows of March–April 2017 and no other row; the count is unchanged
  3. rerun        the same backfill again: same rows, no duplicates (unique order_id holds)

"Rewritten" is read from Postgres row versions (``xmin``): a row the merge replaced gets a new one,
a row it did not touch keeps its own. The file side of P6 (an unchanged re-delivery is skipped by
the landing gate) is asserted in ``make prove-p14``. Run: ``make prove-p6``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import psycopg

REPO = Path(__file__).resolve().parents[1]
PORT = "sales_silver.orders"
START, END = "2017-03-01", "2017-05-01"

failures: list[str] = []


def check(ok: bool, what: str, detail: str = "") -> bool:
    print(f"  {'OK  ' if ok else 'FAIL'} {what}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(what)
    return ok


def versions() -> dict[str, tuple[str, bool]]:
    """order_id -> (row version, inside the backfill window)."""
    with psycopg.connect(
        f"host={os.environ.get('ANALYTICS_HOST', 'localhost')} "
        f"port={os.environ.get('ANALYTICS_PORT', '5433')} "
        f"dbname={os.environ.get('ANALYTICS_DB', 'analytics')} "
        f"user=sales_role password={os.environ['SALES_ROLE_PASSWORD']}"
    ) as c:
        rows = c.execute(
            f"SELECT order_id, xmin::text, order_purchase_timestamp >= %s"
            f" AND order_purchase_timestamp < %s FROM {PORT}",
            (START, END),
        ).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


def publish(*extra: str) -> tuple[int, str]:
    r = subprocess.run(
        [sys.executable, str(REPO / "platform" / "transform" / "publish.py"),
         "--domain", "sales", "--no-state", *extra],
        cwd=REPO, capture_output=True, text=True,
    )  # fmt: skip
    return r.returncode, r.stdout + r.stderr


def changed(before: dict, after: dict) -> tuple[set, set]:
    """(rows rewritten inside the window, rows rewritten outside it)."""
    moved = {k for k in after if k in before and after[k][0] != before[k][0]}
    return {k for k in moved if after[k][1]}, {k for k in moved if not after[k][1]}


def main() -> int:
    try:
        v0 = versions()
    except psycopg.OperationalError as e:
        sys.exit(f"analytics warehouse unreachable — `make up` first ({e})")
    in_window = {k for k, (_, w) in v0.items() if w}
    print(f"   {len(v0)} orders published; {len(in_window)} in {START} .. {END}")

    print("1. Normal run: only the latest batches are processed")
    rc, log = publish()
    v1 = versions()
    inside, outside = changed(v0, v1)
    check(rc == 0 and "merged batches" in log, "audited and merged the latest window")
    check(
        not inside and not outside, "no historical row rewritten", f"{len(inside | outside)} rows"
    )

    print("2. Backfill: exactly the requested range is reprocessed")
    rc, log = publish("--event-time-start", START, "--event-time-end", END)
    v2 = versions()
    inside, outside = changed(v1, v2)
    check(rc == 0 and f"merged batches {START}" in log, "backfill audited and merged", "")
    check(inside == in_window, "every row of the window rewritten", f"{len(inside)} rows")
    check(not outside, "no row outside the window touched", f"{len(outside)} rows")
    check(len(v2) == len(v0), "row count unchanged", f"{len(v2)} rows")

    print("3. Rerun the same backfill: idempotent")
    rc, _ = publish("--event-time-start", START, "--event-time-end", END)
    v3 = versions()
    inside, outside = changed(v2, v3)
    check(rc == 0 and len(v3) == len(v0), "same rows, no duplicates", f"{len(v3)} rows")
    check(inside == in_window and not outside, "again only the window rewritten")

    if failures:
        print(f"\nP6 FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print(
        "\nP6 proven: a normal run touches only the latest batches, a backfill rewrites exactly the"
        " requested range, and running it again changes nothing but those rows' versions."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
