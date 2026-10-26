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

from .rules import ColumnRule, Contract


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


class _ColumnCheck:
    """One rule on one column: it sees every value in the single pass over the file, counts the
    bad ones, and reports an assertion at the end."""

    check = ""
    problem = ""

    def __init__(self, rule: ColumnRule) -> None:
        self.rule = rule
        self.bad = 0

    def observe(self, value: str | None) -> None:
        if value is not None and not _is_null(value) and not self.accepts(value):
            self.bad += 1

    def accepts(self, value: str) -> bool:
        raise NotImplementedError

    def assertion(self) -> Assertion:
        ok = self.bad == 0
        return Assertion(
            self.check, self.rule.name, ok, "ok" if ok else f"{self.bad} {self.problem}"
        )


class _NotNull(_ColumnCheck):
    check, problem = "not_null", "null values"

    def observe(self, value: str | None) -> None:
        if _is_null(value):
            self.bad += 1


class _Unique(_ColumnCheck):
    check, problem = "unique", "duplicate values"

    def __init__(self, rule: ColumnRule) -> None:
        super().__init__(rule)
        self.seen: set[str] = set()

    def accepts(self, value: str) -> bool:
        if value in self.seen:
            return False
        self.seen.add(value)
        return True


class _Integer(_ColumnCheck):
    check, problem = "type_integer", "non-integer values"

    def accepts(self, value: str) -> bool:
        try:
            int(value)
        except (TypeError, ValueError):
            return False
        return True


class _InRange(_ColumnCheck):
    check = "range"

    def __init__(self, rule: ColumnRule) -> None:
        super().__init__(rule)
        self.problem = f"values outside [{rule.minimum}, {rule.maximum}]"

    def accepts(self, value: str) -> bool:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False
        low, high = self.rule.minimum, self.rule.maximum
        return (low is None or number >= low) and (high is None or number <= high)


def _column_checks(rules: list[ColumnRule], present: set[str]) -> list[_ColumnCheck]:
    """The column checks the contract asks for, on the columns the file has, grouped by kind."""
    rules = [r for r in rules if r.name in present]
    return [
        *(_NotNull(r) for r in rules if r.required),
        *(_Unique(r) for r in rules if r.unique),
        *(_Integer(r) for r in rules if r.logical_type == "integer"),
        *(_InRange(r) for r in rules if r.minimum is not None or r.maximum is not None),
    ]


def validate_file(csv_path: Path, contract: Contract) -> ValidationResult:
    with csv_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        present = set(reader.fieldnames or [])
        checks = _column_checks(contract.columns, present)
        rows = 0
        for record in reader:
            rows += 1
            for check in checks:
                check.observe(record.get(check.rule.name))

    missing = [c.name for c in contract.columns if c.name not in present]
    assertions = [
        Assertion(
            "schema.columns_present",
            None,
            not missing,
            "all declared columns present" if not missing else f"missing columns: {missing}",
        )
    ]
    if contract.min_rows is not None:
        ok = rows > contract.min_rows
        detail = (
            f"{rows} rows" if ok else f"only {rows} rows (contract requires > {contract.min_rows})"
        )
        assertions.append(Assertion("completeness.row_count", None, ok, detail))
    assertions += [check.assertion() for check in checks]
    return ValidationResult(
        passed=all(a.passed for a in assertions), rows=rows, assertions=assertions
    )
