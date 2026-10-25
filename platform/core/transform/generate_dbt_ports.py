#!/usr/bin/env python3
"""Generate the dbt metadata for output ports from their ODCS contracts.

Clean separation of generated vs hand-authored dbt YAML (ADR-0012):

  <layer>/_ports.yml   GENERATED here from the output-port contracts (schema,
                       tests, the output_port tag). Never hand-edit it.
  <layer>/_models.yml  HAND-authored minimum metadata for internal (non-port)
                       models. This tool never touches it.

To publish a port a team writes only the ODCS contract (with the output_port tag)
and the dbt model SQL; this tool seeds the model into its layer's _ports.yml and
runs `datacontract dbt sync` to fill it. Idempotent.

dbt sync is given the contracts *resolved* (linked definitions inlined,
dgp_platform_contracts.odcs): the CLI's sync reads a contract as written, so a column that takes
its description and classification from a shared definition would otherwise reach dbt — and the
catalog — without them.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml
from dgp_platform_contracts import odcs

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))
from governance.registry import column_tags, domain_names, tags  # noqa: E402


def _dbt_domains() -> list[str]:
    """The registered domains (governance/domains/) that have a dbt project."""
    return [
        d
        for d in domain_names()
        if (REPO_ROOT / "domains" / d / "dbt" / "dbt_project.yml").exists()
    ]


def _output_port_contracts(domain: str) -> list[tuple[str, Path]]:
    """(product, contract_path) for each output-port contract in a domain."""
    out: list[tuple[str, Path]] = []
    for c in sorted((REPO_ROOT / "domains" / domain / "contracts").glob("*.odcs.yaml")):
        doc = odcs.load(c)
        schema = (doc.get("schema") or [{}])[0]
        if "output_port" in (schema.get("tags") or []):
            out.append((schema.get("name", c.stem), c))
    return out


def _resolved_copy(contract: Path, tmp: Path) -> str:
    """The contract with its linked definitions inlined, written under ``tmp`` (same file name)."""
    out = tmp / contract.name
    out.write_text(yaml.safe_dump(odcs.load(contract), sort_keys=False), encoding="utf-8")
    return str(out)


def _layer_dir(domain: str, product: str) -> Path | None:
    for p in (REPO_ROOT / "domains" / domain / "dbt" / "models").rglob(f"{product}.sql"):
        return p.parent
    return None


def _seed_ports_yml(layer_dir: Path, product: str) -> None:
    """Ensure the layer's _ports.yml lists the port, so sync writes there."""
    f = layer_dir / "_ports.yml"
    doc = yaml.safe_load(f.read_text(encoding="utf-8")) if f.exists() else {}
    models = doc.get("models") or []
    if any(m.get("name") == product for m in models):
        return
    models.append({"name": product})
    f.write_text(
        yaml.safe_dump({"version": 2, "models": models}, sort_keys=False), encoding="utf-8"
    )


def main() -> int:
    datacontract = shutil.which("datacontract")
    if datacontract is None:
        print("datacontract CLI not found (dev group)", file=sys.stderr)
        return 1

    for domain in _dbt_domains():
        contracts = _output_port_contracts(domain)
        if not contracts:
            continue
        for product, _ in contracts:
            layer = _layer_dir(domain, product)
            if layer is None:
                print(f"  warning: no dbt model for port {domain}.{product}", file=sys.stderr)
                continue
            _seed_ports_yml(layer, product)
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [
                    datacontract,
                    "dbt",
                    "sync",
                    *[_resolved_copy(c, Path(tmp)) for _, c in contracts],
                    "--project-dir",
                    str(REPO_ROOT / "domains" / domain / "dbt"),
                ],
                capture_output=True,
                text=True,
            )
        if proc.returncode != 0:
            print(proc.stdout + proc.stderr, file=sys.stderr)
            return proc.returncode
        for product, contract in contracts:
            if (layer := _layer_dir(domain, product)) is not None:
                _sync_deprecation(layer, product, contract)
                _sync_governance_tags(layer, product, contract)
            _prune_stale_tests(domain, contract)
        print(f"  {domain}: synced {len(contracts)} output port(s) into _ports.yml")
    return 0


def _prune_stale_tests(domain: str, contract: Path) -> None:
    """Drop generated contract tests of a superseded version. ``datacontract dbt sync`` names each
    file ``<id>__<version>__...`` and never removes the old version's, so a bump would run the
    same check twice (once per version)."""
    doc = odcs.load(contract)
    slug, version = str(doc.get("id", "")).replace(".", "_"), str(doc.get("version", ""))
    tests = REPO_ROOT / "domains" / domain / "dbt" / "tests" / "datacontract_cli" / slug
    current = f"{slug}__{version.replace('.', '_')}__"
    for f in tests.glob(f"{slug}__*__*.sql") if slug and version else []:
        generated = '"generated": true' in f.read_text(encoding="utf-8")
        if generated and not f.name.startswith(current):
            f.unlink()
            print(f"  {domain}: removed stale generated test {f.name}")


def _sync_governance_tags(layer_dir: Path, product: str, contract: Path) -> None:
    """The port's governed tags (governance/tags/) as {warehouse tag: value} in dbt meta — on each
    column from its resolved contract, and on the model the most sensitive column's sensitivity —
    for the post-hook dgp_dbt.apply_governance_tags to set as part of the build (ADR-0027)."""
    governed = tags()
    warehouse = {
        name: {v["value"]: v["warehouse_value"] for v in tag["values"]}
        for name, tag in governed.items()
    }
    doc = odcs.load(contract)
    columns: dict[str, dict[str, str]] = {}
    for obj in doc.get("schema") or []:
        for prop in obj.get("properties") or []:
            if prop.get("name"):
                columns[prop["name"]] = column_tags(prop)
    table: dict[str, str] = {}
    for name, tag in governed.items():
        if (tag.get("odcs") or {}).get("field") != "classification":
            continue  # only sensitivity has a table-level meaning (privacy is about a column)
        ranked = [v["value"] for v in tag["values"]]  # most sensitive first
        present = [values[name] for values in columns.values() if name in values]
        if present:
            table[tag["warehouse_tag"]] = warehouse[name][min(present, key=ranked.index)]
    f = layer_dir / "_ports.yml"
    ports = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    for model in ports.get("models") or []:
        if model.get("name") != product:
            continue
        model.setdefault("config", {}).setdefault("meta", {})["governance_tags"] = table
        for col in model.get("columns") or []:
            values = columns.get(col.get("name"), {})
            col.setdefault("config", {}).setdefault("meta", {})["governance_tags"] = {
                governed[name]["warehouse_tag"]: warehouse[name][value]
                for name, value in values.items()
            }
    f.write_text(yaml.safe_dump(ports, sort_keys=False), encoding="utf-8")


def _sync_deprecation(layer_dir: Path, product: str, contract: Path) -> None:
    """A deprecated port version's sunset becomes dbt's ``deprecation_date`` (ADR-0019 §3), so dbt
    warns every model that still refs it; an active port carries none."""
    doc = odcs.load(contract)
    props = {p.get("property"): p.get("value") for p in doc.get("customProperties") or []}
    sunset = props.get("sunsetDate") if doc.get("status") == "deprecated" else None
    f = layer_dir / "_ports.yml"
    ports = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    changed = False
    for m in ports.get("models") or []:
        if m.get("name") != product:
            continue
        want = f"{sunset} 00:00:00+00:00" if sunset else None
        if m.get("deprecation_date") != want:
            changed = True
            if want:
                m["deprecation_date"] = want
            else:
                m.pop("deprecation_date", None)
    if changed:
        f.write_text(yaml.safe_dump(ports, sort_keys=False), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
