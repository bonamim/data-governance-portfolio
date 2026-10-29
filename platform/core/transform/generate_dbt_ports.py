#!/usr/bin/env python3
"""Generate the dbt metadata for output ports from their ODCS contracts.

Clean separation of generated vs hand-authored dbt YAML (ADR-0012):

  <layer>/_ports.yml   GENERATED here from the output-port contracts (schema,
                       tests, the output_port tag). Never hand-edit it.
  <layer>/_models.yml  HAND-authored minimum metadata for internal (non-port)
                       models. This tool never touches it.
  <layer>/_sources.yml HAND-authored sources, except the columns of every raw table
                       the platform loads: the platform's system columns (ADR-0028)
                       and, for one loaded from a landing contract, the table's and
                       its columns' descriptions from that contract (resolved), so
                       the dbt source and its warehouse table say the same thing.

To publish a port a team writes only the ODCS contract (with the output_port tag)
and the dbt model SQL. This tool seeds the model into its layer's _ports.yml and
runs `datacontract dbt sync` to fill it. Idempotent.

dbt sync is given the contracts *resolved* (linked definitions inlined,
dgp_platform_contracts.odcs): the CLI's sync reads a contract as written, so a column that takes
its description and classification from a shared definition would otherwise reach dbt (and the
catalog) without them.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml
from dgp_governance import products, registry
from dgp_governance.repo import load_yaml, root
from dgp_platform_contracts import odcs
from dgp_platform_ingestion.config import load_datasets
from dgp_platform_quality.rules import contract_path


def _dbt_domains() -> list[str]:
    """The registered domains (governance/domains/) that have a dbt project."""
    return [
        d
        for d in registry.domain_names()
        if (root() / "domains" / d / "dbt" / "dbt_project.yml").exists()
    ]


def _resolved_copy(port: products.OutputPort, tmp: Path) -> str:
    """The port's contract, linked definitions inlined, written under ``tmp`` (same file name)."""
    out = tmp / port.contract.name
    out.write_text(yaml.safe_dump(port.document, sort_keys=False), encoding="utf-8")
    return str(out)


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


def _sync(datacontract: str, domain: str, ports: list[products.OutputPort]) -> int:
    """``datacontract dbt sync`` for one domain's ports, given resolved copies. Its exit code."""
    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.run(
            [
                datacontract,
                "dbt",
                "sync",
                *[_resolved_copy(p, Path(tmp)) for p in ports],
                "--project-dir",
                str(root() / "domains" / domain / "dbt"),
            ],
            capture_output=True,
            text=True,
        )
    if proc.returncode != 0:
        print(proc.stdout + proc.stderr, file=sys.stderr)
    return proc.returncode


def main() -> int:
    datacontract = shutil.which("datacontract")
    if datacontract is None:
        print("datacontract CLI not found (dev group)", file=sys.stderr)
        return 1

    for domain in _dbt_domains():
        ports = sorted(products.output_ports(domain).values(), key=lambda p: p.contract)
        if not ports:
            continue
        for port in ports:
            if port.model_file is None:
                print(f"  warning: no dbt model for port {domain}.{port.name}", file=sys.stderr)
                continue
            _seed_ports_yml(port.model_file.parent, port.name)
        if code := _sync(datacontract, domain, ports):
            return code
        for port in ports:
            if port.model_file is not None:
                _sync_deprecation(port.model_file.parent, port)
                _sync_governance_tags(port.model_file.parent, port)
            _prune_stale_tests(domain, port)
        print(f"  {domain}: synced {len(ports)} output port(s) into _ports.yml")
    _sync_raw_sources()
    return 0


def _sync_raw_sources() -> None:
    """Describe every dbt source table the platform loads (a raw table): its columns, the
    platform's system columns after them, and for a landing dataset the table's description and
    each column's from its resolved landing contract."""
    if not any(root().glob("domains/*/ingestion/*.yml")):
        return  # no source config, so nothing is loaded
    datasets = load_datasets(root())
    loaded = {(d.raw_schema, d.raw_table) for d in datasets}
    contracts = {
        (d.raw_schema, d.raw_table): path
        for d in datasets
        if d.source_type == "landing" and (path := contract_path(root(), d.domain, d.name)).exists()
    }
    system = [
        {"name": c["name"], "description": c.get("description") or ""}
        for c in registry.system_columns()
    ]
    for domain in _dbt_domains():
        for f in sorted((root() / "domains" / domain / "dbt" / "models").rglob("_sources.yml")):
            doc = load_yaml(f)
            before = yaml.safe_dump(doc, sort_keys=False)
            for source in doc.get("sources") or []:
                for table in source.get("tables") or []:
                    key = (source.get("schema", source["name"]), table["name"])
                    if key not in loaded:
                        continue
                    columns: list[dict] = []
                    if (path := contracts.get(key)) is not None:
                        schema = (odcs.load(path).get("schema") or [{}])[0]
                        table["description"] = schema.get("description") or ""
                        columns = [
                            {"name": c["name"], "description": c.get("description") or ""}
                            for c in schema.get("properties") or []
                            if c.get("name")
                        ]
                    table["columns"] = columns + system
            after = yaml.safe_dump(doc, sort_keys=False)
            if after != before:
                f.write_text(after, encoding="utf-8")
                print(f"  {domain}: described raw sources in {f.name}")


def _prune_stale_tests(domain: str, port: products.OutputPort) -> None:
    """Drop generated contract tests of a superseded version. ``datacontract dbt sync`` names each
    file ``<id>__<version>__...`` and never removes the old version's, so a bump would run the
    same check twice (once per version)."""
    doc = port.document
    slug, version = str(doc.get("id", "")).replace(".", "_"), str(doc.get("version", ""))
    tests = root() / "domains" / domain / "dbt" / "tests" / "datacontract_cli" / slug
    current = f"{slug}__{version.replace('.', '_')}__"
    for f in tests.glob(f"{slug}__*__*.sql") if slug and version else []:
        generated = '"generated": true' in f.read_text(encoding="utf-8")
        if generated and not f.name.startswith(current):
            f.unlink()
            print(f"  {domain}: removed stale generated test {f.name}")


def _sync_governance_tags(layer_dir: Path, port: products.OutputPort) -> None:
    """The port's governed tags (governance/tags/) as {warehouse tag: value} in dbt meta (on each
    column from its resolved contract, and on the model the most sensitive column's sensitivity),
    for the post-hook dgp_dbt.apply_governance_tags to set as part of the build (ADR-0027)."""
    table, columns = registry.warehouse_tags(port.columns)
    f = layer_dir / "_ports.yml"
    ports = load_yaml(f)
    for model in ports.get("models") or []:
        if model.get("name") != port.name:
            continue
        model.setdefault("config", {}).setdefault("meta", {})["governance_tags"] = table
        for col in model.get("columns") or []:
            col.setdefault("config", {}).setdefault("meta", {})["governance_tags"] = columns.get(
                col.get("name"), {}
            )
    f.write_text(yaml.safe_dump(ports, sort_keys=False), encoding="utf-8")


def _sync_deprecation(layer_dir: Path, port: products.OutputPort) -> None:
    """A deprecated port version's sunset becomes dbt's ``deprecation_date`` (ADR-0019 §3), so dbt
    warns every model that still refs it. An active port carries none."""
    sunset = port.sunset if port.status == "deprecated" else None
    f = layer_dir / "_ports.yml"
    ports = load_yaml(f)
    changed = False
    for m in ports.get("models") or []:
        if m.get("name") != port.name:
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
