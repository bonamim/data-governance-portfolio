#!/usr/bin/env python3
"""Federated governance policy: every landing dataset must have a valid ODCS contract.

The landing zone is a governed boundary (docs/data-mesh.md): files are consumed by
many data products, so each landing dataset must declare an ODCS contract, and that
contract must be valid. This fails CI when a landing dataset has no contract, or when
its contract does not pass `datacontract lint`.

Output-port (table/view) contracts are checked by the same policy once data products
exist (M3); for now it governs the landing boundary.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def _source_files() -> list[Path]:
    paths = sorted(REPO_ROOT.glob("domains/*/ingestion/sources.yml"))
    reference = REPO_ROOT / "reference" / "ingestion" / "sources.yml"
    if reference.exists():
        paths.append(reference)
    return paths


def _landing_datasets() -> list[tuple[str, str]]:
    """Return (domain, dataset) for every landing-sourced dataset in the repo."""
    datasets: list[tuple[str, str]] = []
    for path in _source_files():
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        domain = doc.get("domain")
        for entry in doc.get("datasets", []):
            if (entry.get("source") or {}).get("type") == "landing":
                datasets.append((domain, entry["name"]))
    return datasets


def _contract_path(domain: str, dataset: str) -> Path:
    base = (REPO_ROOT / "reference") if domain == "reference" else REPO_ROOT / "domains" / domain
    return base / "contracts" / f"{dataset}.odcs.yaml"


def _lint(path: Path) -> tuple[bool, str]:
    datacontract = shutil.which("datacontract")
    if datacontract is None:
        return False, "datacontract CLI not found (add datacontract-cli to the dev group)"
    proc = subprocess.run([datacontract, "lint", str(path)], capture_output=True, text=True)
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


def main() -> int:
    violations: list[str] = []
    datasets = _landing_datasets()
    for domain, dataset in datasets:
        path = _contract_path(domain, dataset)
        rel = path.relative_to(REPO_ROOT)
        if not path.exists():
            violations.append(f"{domain}.{dataset}: no ODCS contract at {rel}")
            continue
        ok, output = _lint(path)
        if not ok:
            detail = output.splitlines()[-1] if output else ""
            violations.append(f"{rel}: ODCS contract is invalid\n      {detail}")

    if violations:
        print("Contract governance violations:\n")
        for v in violations:
            print(f"  - {v}")
        print(f"\n{len(violations)} violation(s). See docs/data-mesh.md (governed boundaries).")
        return 1
    print(f"Contract governance OK: {len(datasets)} landing dataset(s), all with valid ODCS.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
