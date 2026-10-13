#!/usr/bin/env python3
"""Federated governance policy: data product definitions meet the standard.

Enforces the machine-checkable data product heuristics from
governance/standards/data-product.md (a valid product has a purpose, a type, an
owner, a named consumer with a use case, output ports backed by valid ODCS
contracts, and — for aggregates — at least three consumers).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
from governance.registry import domain_names  # noqa: E402

DOMAINS = set(domain_names())  # single source of truth: governance/domains/
TYPES = {"source-aligned", "aggregate", "consumer-aligned"}
INPUT_RE = re.compile(r"^[a-z_]+\.[a-z_]+$")  # <domain>.<product>


def _definition_files() -> list[Path]:
    paths = sorted(REPO_ROOT.glob("domains/*/data_products/*.yaml"))
    paths += sorted(REPO_ROOT.glob("reference/data_products/*.yaml"))
    return paths


def _lint_contract(path: Path) -> tuple[bool, str]:
    datacontract = shutil.which("datacontract")
    if datacontract is None:
        return False, "datacontract CLI not found"
    proc = subprocess.run([datacontract, "lint", str(path)], capture_output=True, text=True)
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


def _check(defn: dict, path: Path) -> list[str]:
    where = path.relative_to(REPO_ROOT)
    errs: list[str] = []

    purpose = defn.get("purpose", "")
    if not isinstance(purpose, str) or len(purpose.split()) < 3:
        errs.append(f"{where}: 'purpose' must be a one-sentence verb + object")

    dptype = defn.get("type")
    if dptype not in TYPES:
        errs.append(f"{where}: 'type' must be one of {sorted(TYPES)} (got {dptype!r})")

    owner = defn.get("owner") or {}
    if owner.get("team") not in DOMAINS:
        errs.append(f"{where}: 'owner.team' must be a known domain (got {owner.get('team')!r})")

    consumers = defn.get("consumers") or []
    with_use_case = [c for c in consumers if (c or {}).get("use_case")]
    if not with_use_case:
        errs.append(f"{where}: needs a consumer with a use_case (no use case, no product)")
    if dptype == "aggregate" and len(with_use_case) < 3:
        errs.append(f"{where}: aggregate needs >= 3 consuming teams (has {len(with_use_case)})")

    for port in defn.get("output_ports") or []:
        contract = (port or {}).get("contract")
        if not contract:
            errs.append(f"{where}: an output_port is missing 'contract'")
            continue
        cpath = REPO_ROOT / contract
        if not cpath.exists():
            errs.append(f"{where}: output_port contract not found: {contract}")
            continue
        ok, output = _lint_contract(cpath)
        if not ok:
            last = output.splitlines()[-1] if output else ""
            errs.append(f"{contract}: invalid ODCS contract\n      {last}")

    for ref in defn.get("inputs") or []:
        if not INPUT_RE.match(str(ref)):
            errs.append(f"{where}: input '{ref}' must be a <domain>.<product> output port")

    return errs


def main() -> int:
    files = _definition_files()
    violations: list[str] = []
    for path in files:
        defn = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        violations.extend(_check(defn, path))

    if violations:
        print("Data product governance violations:\n")
        for v in violations:
            print(f"  - {v}")
        print(f"\n{len(violations)} violation(s). See governance/standards/data-product.md")
        return 1
    print(f"Data product governance OK: {len(files)} product(s), all valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
