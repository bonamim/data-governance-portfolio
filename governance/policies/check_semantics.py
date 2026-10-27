#!/usr/bin/env python3
"""The semantic layer is built only on governed output-port columns (ADR-0010).

Every Apache Ossie semantic model (``domains/*/semantics/*.semantic_model.yaml``) reads only
**governed output-port columns**: each dataset's ``source`` is an output port with an ODCS
contract, and every field and metric uses columns that exist in that contract. So the numbers
Superset and the AI agents compute can't drift from the data contract.

A field is either a contract column (named as the column) or a **derived** field: any other name,
whose expression reads only contract columns as ``<dataset>.<column>``, for example
``order_purchase_month`` = ``DATE_TRUNC('month', orders.order_purchase_timestamp)``.

Every dataset, derived field and metric needs a ``description``. The semantic agents hand these to
the model verbatim (``list_metrics``), and an agent with no description guesses the meaning from the
name, counting ``customer_id`` (one per order) as people, for instance. A field that IS a contract
column must not have one: its description is the contract's (and its data element's), and the
compile step (``ossie.compile_model``) puts it into the model every consumer reads, so a column is
described in one place (ADR-0027). The model must also pass Ossie's own validator.
"""

from __future__ import annotations

import sys
from pathlib import Path

from _checks import report
from dgp_governance import checks, products
from dgp_governance.repo import relative
from dgp_platform_semantics import ossie


def _described(node: dict) -> bool:
    return bool(str(node.get("description") or "").strip())


def dataset_violations(dataset: dict, port: products.OutputPort, where: str) -> list[str]:
    """What one dataset gets wrong, against its output port's columns."""
    name, columns = dataset.get("name"), port.column_names
    errs = [] if _described(dataset) else [f"{where}: dataset '{name}' has no description"]
    ungoverned: set[str] = set()
    for field in ossie.fields(dataset):
        field_name = field.get("name")
        if field_name in columns:
            if _described(field):  # one place per fact (ADR-0027)
                errs.append(
                    f"{where}: field '{field_name}' is a contract column. Remove its "
                    "description, which is the contract's (and its data element's), inherited"
                )
            continue
        if not _described(field):
            errs.append(f"{where}: derived field '{field_name}' has no description")
        refs = ossie.referenced_columns(field)  # a derived field reads contract columns only
        if not field_name or not refs or refs - columns:
            ungoverned.add(str(field_name))
    if ungoverned:
        errs.append(
            f"{where}: dataset '{name}' has fields neither in the contract nor derived only from "
            f"contract columns: {sorted(ungoverned)}"
        )
    return errs


def model_violations(path: Path) -> list[str]:
    """What one semantic model gets wrong."""
    where = relative(path)
    spec = ossie.load(path)
    errs = [f"{where}: {e}" for e in ossie.validate(spec, ossie.MODEL_SCHEMA)]
    if not spec.get("name") or not ossie.datasets(spec):
        return [*errs, f"{where}: missing top-level 'name' or 'datasets'"]

    governed: set[str] = set()
    for dataset in ossie.datasets(spec):
        table = ossie.source_table(dataset)
        port = products.output_port(table)
        if port is None or not port.column_names:
            errs.append(
                f"{where}: dataset '{dataset.get('name')}' source '{dataset.get('source')}' is not "
                f"a governed output port (no output-port ODCS contract for '{table}')"
            )
            continue
        governed |= port.column_names
        errs += dataset_violations(dataset, port, where)

    for metric in ossie.metrics(spec):
        if not _described(metric):
            errs.append(f"{where}: metric '{metric.get('name')}' has no description")
        if bad := ossie.referenced_columns(metric) - governed:
            errs.append(
                f"{where}: metric '{metric.get('name')}' references non-contract columns: "
                f"{sorted(bad)}"
            )
    return errs


def main() -> int:
    models = ossie.models()
    return report(
        [v for m in models for v in model_violations(m)],
        name="Semantic models",
        failed="Semantic governance FAILED",
        ok=f"{checks.plural(len(models), 'model')}, all on contract columns and described",
    )


if __name__ == "__main__":
    sys.exit(main())
