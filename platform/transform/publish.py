#!/usr/bin/env python3
"""Write-audit-publish orchestrator (ADR-0014/0015, proof P3).

This only *sequences* dbt — all the publish SQL lives in the `dgp_dbt` package macros
(`wap_publish` / `wap_drop_candidates`), so there is no hand-rolled SQL outside the dbt
projects. For each domain:

  1. ``dbt deps``                                 install the shared macros
  2. ``dbt build --vars '{wap: true, …}'``        WRITE opted-in table ports to
                                                  ``<name>__candidate`` and AUDIT them
                                                  (the contract DQ runs in the build)
  3. pass → ``dbt run-operation wap_publish``          PUBLISH: dependency-safe atomic swap
     fail → ``dbt run-operation wap_drop_candidates``  QUARANTINE: drop candidates, keep
                                                       consumers on the last good version

Opt-in is per data product (``publish: {audit: required}``); everything else builds in place.
Run: ``make publish``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PROFILES = REPO_ROOT / "platform" / "transform"
DOMAINS = ["customer", "customer_experience", "sales"]  # dependency order


def _wap_products() -> dict[str, list[str]]:
    """domain -> [product, …] for every product opting in to WAP (publish.audit == required)."""
    out: dict[str, list[str]] = {}
    for p in REPO_ROOT.glob("domains/*/data_products/*.yaml"):
        defn = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if (defn.get("publish") or {}).get("audit") == "required":
            out.setdefault(p.relative_to(REPO_ROOT).parts[1], []).append(defn["name"])
    return out


def _dbt(domain: str, *args: str) -> int:
    dbt = shutil.which("dbt") or "dbt"
    cwd = REPO_ROOT / "domains" / domain / "dbt"  # cwd so dbt-loom finds its config
    return subprocess.run([dbt, *args, "--profiles-dir", str(PROFILES)], cwd=cwd).returncode


def _grant() -> None:
    """Re-apply consumer grants (a swap/rebuild drops table grants)."""
    subprocess.run([sys.executable, str(REPO_ROOT / "platform" / "access" / "grant_consumers.py")])


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Write-audit-publish output ports.")
    parser.add_argument("--domain", help="publish only this domain (default: all, in order)")
    args = parser.parse_args()
    domains = [args.domain] if args.domain else DOMAINS

    wap = _wap_products()
    exit_code = 0
    for domain in domains:
        products = wap.get(domain, [])
        _dbt(domain, "deps")
        wap_vars = "{wap: true, wap_products: [" + ", ".join(products) + "]}"
        rc = _dbt(domain, "build", "--vars", wap_vars)
        if rc == 0:
            op_args = json.dumps({"domain": domain, "products": products})
            if _dbt(domain, "run-operation", "wap_publish", "--args", op_args) != 0:
                exit_code = 1
        else:
            op_args = json.dumps({"domain": domain})
            _dbt(domain, "run-operation", "wap_drop_candidates", "--args", op_args)
            print(f"  QUARANTINED {domain}: audit failed — kept last good, dropped candidate(s)")
            exit_code = 1
        # Grant after each domain so the next one can read its upstream ports.
        _grant()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
