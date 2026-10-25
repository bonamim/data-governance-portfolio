"""Execute an ODCS contract's column rules against a CSV file.

One streaming pass (so a million-row landing file is fine). Produces a
ValidationResult of per-rule assertions; the landing gate quarantines the file if
any assertion fails (proof P2). Rules checked: declared columns present, not-null
on required columns, uniqueness, value range, and integer type.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from .rules import Contract


@dataclass(frozen=True)
class Assertion:
    check: str
    column: str | None
    passed: bool
    detail: str


@dataclass(frozen=True)
class ValidationResult:
    passed: bool
    rows: int
    assertions: list[Assertion]

    def failures(self) -> list[Assertion]:
        return [a for a in self.assertions if not a.passed]

    def summary(self) -> str:
        fails = self.failures()
        if not fails:
            return f"all {len(self.assertions)} checks passed on {self.rows} rows"
        return "; ".join(f"{a.check}({a.column}): {a.detail}" for a in fails)


def _is_null(value: str | None) -> bool:
    return value is None or value.strip() == ""


def validate_file(csv_path: Path, contract: Contract) -> ValidationResult:
    rules = {c.name: c for c in contract.columns}

    with csv_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames or []
        present = set(header)

        missing = [name for name in rules if name not in present]

        required_cols = [n for n, r in rules.items() if r.required and n in present]
        unique_cols = [n for n, r in rules.items() if r.unique and n in present]
        ranged_cols = [
            n
            for n, r in rules.items()
            if (r.minimum is not None or r.maximum is not None) and n in present
        ]
        int_cols = [n for n, r in rules.items() if r.logical_type == "integer" and n in present]

        null_counts = dict.fromkeys(required_cols, 0)
        seen: dict[str, set] = {n: set() for n in unique_cols}
        dup_counts = dict.fromkeys(unique_cols, 0)
        range_bad = dict.fromkeys(ranged_cols, 0)
        type_bad = dict.fromkeys(int_cols, 0)

        rows = 0
        for record in reader:
            rows += 1
            for col in required_cols:
                if _is_null(record.get(col)):
                    null_counts[col] += 1
            for col in unique_cols:
                val = record.get(col)
                if not _is_null(val):
                    if val in seen[col]:
                        dup_counts[col] += 1
                    else:
                        seen[col].add(val)
            for col in int_cols:
                val = record.get(col)
                if not _is_null(val):
                    try:
                        int(val)
                    except (TypeError, ValueError):
                        type_bad[col] += 1
            for col in ranged_cols:
                val = record.get(col)
                if _is_null(val):
                    continue
                try:
                    num = float(val)
                except (TypeError, ValueError):
                    range_bad[col] += 1
                    continue
                rule = rules[col]
                if (rule.minimum is not None and num < rule.minimum) or (
                    rule.maximum is not None and num > rule.maximum
                ):
                    range_bad[col] += 1

    assertions: list[Assertion] = []
    assertions.append(
        Assertion(
            "schema.columns_present",
            None,
            not missing,
            "all declared columns present" if not missing else f"missing columns: {missing}",
        )
    )
    if contract.min_rows is not None:
        ok = rows > contract.min_rows
        assertions.append(
            Assertion(
                "completeness.row_count",
                None,
                ok,
                f"{rows} rows"
                if ok
                else f"only {rows} rows (contract requires > {contract.min_rows})",
            )
        )
    for col in required_cols:
        n = null_counts[col]
        assertions.append(
            Assertion("not_null", col, n == 0, "ok" if n == 0 else f"{n} null values")
        )
    for col in unique_cols:
        n = dup_counts[col]
        assertions.append(
            Assertion("unique", col, n == 0, "ok" if n == 0 else f"{n} duplicate values")
        )
    for col in int_cols:
        n = type_bad[col]
        assertions.append(
            Assertion("type_integer", col, n == 0, "ok" if n == 0 else f"{n} non-integer values")
        )
    for col in ranged_cols:
        n = range_bad[col]
        rule = rules[col]
        bounds = f"[{rule.minimum}, {rule.maximum}]"
        assertions.append(
            Assertion("range", col, n == 0, "ok" if n == 0 else f"{n} values outside {bounds}")
        )

    return ValidationResult(
        passed=all(a.passed for a in assertions), rows=rows, assertions=assertions
    )
