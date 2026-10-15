#!/usr/bin/env python3
"""Publish the landing zone and its contract-gate results into DataHub (ADR-0024).

For every landing dataset in ``sources.yml`` (contract = source of truth, ledger = system of
record; DataHub is the read model, ADR-0018):

  * an **S3 dataset** ``<archive>/accepted/<source>/<dataset>`` with the contract schema, purpose,
    domain, owner, operational properties (partitioning, naming, retention, completion signal)
    and where its inbox / accepted / quarantine prefixes are;
  * **lineage** landing dataset → ``<domain>_raw.<table>``, joining the warehouse/dbt graph;
  * one **assertion per contract rule** the gate executes (schema, completeness, not-null,
    unique, type, range, file naming);
  * one **assertion run event per delivered file and rule**, from the ingestion ledger: result,
    partition, row count, and where the file was filed (accepted/ or quarantine/ + _reason.json).

S3 datasets that are no longer landing datasets (e.g. names from an older layout) are
soft-deleted, so the catalog matches the repo. Idempotent: re-running upserts.

Runs automatically as the ``publish_quality`` task after ``load_raw`` in each domain's ingestion
DAG (``--domain``), so results reach DataHub right after every gate run. By hand / full reconcile:
``make catalog-quality`` (isolated DataHub SDK env).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import UTC
from pathlib import Path

import psycopg
import yaml
from datahub.emitter.mce_builder import (
    datahub_guid,
    make_assertion_urn,
    make_data_platform_urn,
    make_dataset_urn,
    make_domain_urn,
    make_group_urn,
    make_schema_field_urn,
)
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.rest_emitter import DatahubRestEmitter
from datahub.metadata.schema_classes import (
    AssertionInfoClass,
    AssertionResultClass,
    AssertionResultTypeClass,
    AssertionRunEventClass,
    AssertionRunStatusClass,
    AssertionSourceClass,
    AssertionSourceTypeClass,
    AssertionStdAggregationClass,
    AssertionStdOperatorClass,
    AssertionStdParameterClass,
    AssertionStdParametersClass,
    AssertionStdParameterTypeClass,
    AssertionTypeClass,
    DatasetAssertionInfoClass,
    DatasetAssertionScopeClass,
    DatasetLineageTypeClass,
    DatasetPropertiesClass,
    DateTypeClass,
    DomainsClass,
    NumberTypeClass,
    OtherSchemaClass,
    OwnerClass,
    OwnershipClass,
    OwnershipTypeClass,
    PartitionSpecClass,
    PartitionTypeClass,
    SchemaFieldClass,
    SchemaFieldDataTypeClass,
    SchemaMetadataClass,
    StatusClass,
    StringTypeClass,
    SubTypesClass,
    UpstreamClass,
    UpstreamLineageClass,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
GMS = os.environ.get("DATAHUB_GMS", "http://localhost:8080")
DB = os.environ.get("ANALYTICS_DB", "analytics")
WAREHOUSE_PLATFORM = os.environ.get("WAREHOUSE_PLATFORM", "postgres")
INBOX = os.environ.get("LANDING_INBOX_BUCKET", "landing-inbox")
ARCHIVE = os.environ.get("LANDING_ARCHIVE_BUCKET", "landing-archive")
S3_BROWSER = f"http://localhost:{os.environ.get('MINIO_CONSOLE_PORT', '9001')}"

# The landing layout + contract parsing are the platform's own code (stdlib + pyyaml only), so
# the catalog cannot drift from what the loader does.
sys.path.insert(0, str(REPO_ROOT / "platform" / "ingestion" / "src"))
sys.path.insert(0, str(REPO_ROOT / "platform" / "quality" / "src"))
from dgp_platform_ingestion import layout  # noqa: E402
from dgp_platform_ingestion.layout import LandingRules  # noqa: E402
from dgp_platform_quality.contract import contract_path, load_contract  # noqa: E402

_TYPES = {"integer": NumberTypeClass, "number": NumberTypeClass, "date": DateTypeClass}
P = AssertionStdParameterTypeClass


def _landing_datasets() -> list[dict]:
    paths = sorted(REPO_ROOT.glob("domains/*/ingestion/sources.yml"))
    paths.append(REPO_ROOT / "reference" / "ingestion" / "sources.yml")
    out = []
    for path in paths:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for e in doc.get("datasets", []):
            if (e.get("source") or {}).get("type") == "landing":
                out.append(
                    {
                        "domain": doc["domain"],
                        "source": doc.get("source_system", "olist"),
                        "name": e["name"],
                        "raw_table": (e.get("target") or {}).get("raw_table", e["name"]),
                    }
                )
    return out


def _param(value, kind=P.NUMBER) -> AssertionStdParameterClass:
    return AssertionStdParameterClass(value=str(value), type=kind)


def _rules(contract, rules: LandingRules) -> list[dict]:
    """The checks the gate runs for this contract — mirrors dgp_platform_quality.engine."""
    out = [
        {"check": "schema.columns_present", "column": None, "scope": "schema",
         "op": AssertionStdOperatorClass._NATIVE_, "agg": AssertionStdAggregationClass.COLUMNS,
         "desc": f"File carries every contract column: {', '.join(contract.column_names)}"},
    ]  # fmt: skip
    if rules.filename_pattern is not None:
        out.append(
            {"check": "naming.filename_pattern", "column": None, "scope": "unknown",
             "op": AssertionStdOperatorClass.REGEX_MATCH,
             "agg": AssertionStdAggregationClass._NATIVE_,
             "params": AssertionStdParametersClass(
                 value=_param(rules.filename_pattern.pattern, P.STRING)),
             "desc": f"File name follows the partner convention {rules.filename_pattern.pattern}"}
        )  # fmt: skip
    if contract.min_rows is not None:
        out.append(
            {"check": "completeness.row_count", "column": None, "scope": "rows",
             "op": AssertionStdOperatorClass.GREATER_THAN,
             "agg": AssertionStdAggregationClass.ROW_COUNT,
             "params": AssertionStdParametersClass(value=_param(contract.min_rows)),
             "desc": f"File completeness: more than {contract.min_rows} rows"}
        )  # fmt: skip
    for c in contract.columns:
        if c.required:
            out.append(
                {"check": "not_null", "column": c.name, "scope": "column",
                 "op": AssertionStdOperatorClass.NOT_NULL,
                 "agg": AssertionStdAggregationClass.IDENTITY,
                 "desc": f"{c.name} is required (no empty values)"}
            )  # fmt: skip
        if c.unique:
            out.append(
                {"check": "unique", "column": c.name, "scope": "column",
                 "op": AssertionStdOperatorClass.EQUAL_TO,
                 "agg": AssertionStdAggregationClass.UNIQUE_PROPOTION,
                 "params": AssertionStdParametersClass(value=_param(1)),
                 "desc": f"{c.name} is unique"}
            )  # fmt: skip
        if c.logical_type == "integer":
            out.append(
                {"check": "type_integer", "column": c.name, "scope": "column",
                 "op": AssertionStdOperatorClass._NATIVE_,
                 "agg": AssertionStdAggregationClass.IDENTITY,
                 "desc": f"{c.name} values are integers"}
            )  # fmt: skip
        if c.minimum is not None or c.maximum is not None:
            out.append(
                {"check": "range", "column": c.name, "scope": "column",
                 "op": AssertionStdOperatorClass.BETWEEN,
                 "agg": AssertionStdAggregationClass.IDENTITY,
                 "params": AssertionStdParametersClass(
                     minValue=_param(c.minimum), maxValue=_param(c.maximum)),
                 "desc": f"{c.name} between {c.minimum} and {c.maximum}"}
            )  # fmt: skip
    return out


_SCOPES = {
    "schema": DatasetAssertionScopeClass.DATASET_SCHEMA,
    "rows": DatasetAssertionScopeClass.DATASET_ROWS,
    "column": DatasetAssertionScopeClass.DATASET_COLUMN,
    "unknown": DatasetAssertionScopeClass.UNKNOWN,
}


def _assertion_urn(ds_urn: str, check: str, column: str | None) -> str:
    return make_assertion_urn(
        datahub_guid({"platform": "dgp-landing-gate", "dataset": ds_urn, "check": check,
                      "column": column or ""})
    )  # fmt: skip


def _dataset_mcps(d: dict, contract, rules: LandingRules, ds_urn: str) -> list:
    src, name = d["source"], d["name"]
    props = {
        "contract": f"{contract.id} v{contract.version}",
        "contract_path": str(contract.path.relative_to(REPO_ROOT)),
        "partitionScheme": rules.partition_scheme,
        "partitionSource": rules.partition_source,
        "filenamePattern": rules.filename_pattern.pattern if rules.filename_pattern else "",
        "retentionDays": str(rules.retention_days),
        "completionSignal": rules.completion_signal,
        "inbox": f"s3://{INBOX}/{layout.inbox_prefix(src, name)}",
        "accepted": f"s3://{ARCHIVE}/{layout.accepted_prefix(src, name)}",
        "quarantine": f"s3://{ARCHIVE}/{layout.QUARANTINE}/{src}/{name}/",
    }
    fields = [
        SchemaFieldClass(
            fieldPath=c.name,
            type=SchemaFieldDataTypeClass(type=_TYPES.get(c.logical_type or "", StringTypeClass)()),
            nativeDataType=c.logical_type or "string",
            nullable=not c.required,
        )
        for c in contract.columns
    ]
    raw_urn = make_dataset_urn(WAREHOUSE_PLATFORM, f"{DB}.{d['domain']}_raw.{d['raw_table']}")
    aspects = [
        StatusClass(removed=False),
        SubTypesClass(typeNames=["Landing Dataset"]),
        DatasetPropertiesClass(
            name=f"{src}/{name} (landing)",
            description=(
                f"{contract.purpose or ''}\n\nPartner-delivered files for **{src}/{name}**, "
                f"gated against the ODCS landing contract `{contract.id}` before load. "
                f"Accepted files are immutable evidence ({rules.retention_days}d); rejected files "
                f"go to quarantine with a `_reason.json` (90d). Owner: **{d['domain']}**."
            ).strip(),
            customProperties=props,
            externalUrl=f"{S3_BROWSER}/buckets/{ARCHIVE}",
        ),
        SchemaMetadataClass(
            schemaName=name,
            platform=make_data_platform_urn("s3"),
            version=0,
            hash="",
            platformSchema=OtherSchemaClass(rawSchema=""),
            fields=fields,
        ),
        DomainsClass(domains=[make_domain_urn(d["domain"])]),
        OwnershipClass(
            owners=[
                OwnerClass(
                    owner=make_group_urn(contract.team or d["domain"]),
                    type=OwnershipTypeClass.BUSINESS_OWNER,
                )
            ]
        ),  # fmt: skip
    ]
    mcps = [MetadataChangeProposalWrapper(entityUrn=ds_urn, aspect=a) for a in aspects]
    mcps.append(
        MetadataChangeProposalWrapper(
            entityUrn=raw_urn,
            aspect=UpstreamLineageClass(
                upstreams=[UpstreamClass(dataset=ds_urn, type=DatasetLineageTypeClass.COPY)]
            ),
        )
    )
    return mcps


def _assertion_mcps(contract, rules: LandingRules, ds_urn: str) -> list:
    mcps = []
    for r in _rules(contract, rules):
        info = AssertionInfoClass(
            type=AssertionTypeClass.DATASET,
            description=r["desc"],
            datasetAssertion=DatasetAssertionInfoClass(
                dataset=ds_urn,
                scope=_SCOPES[r["scope"]],
                fields=[make_schema_field_urn(ds_urn, r["column"])] if r["column"] else None,
                operator=r["op"],
                aggregation=r["agg"],
                parameters=r.get("params"),
                nativeType=r["check"],
            ),
            source=AssertionSourceClass(type=AssertionSourceTypeClass.EXTERNAL),
            customProperties={"engine": "dgp-platform-quality (landing gate)",
                              "contract": f"{contract.id} v{contract.version}"},
        )  # fmt: skip
        mcps.append(
            MetadataChangeProposalWrapper(
                entityUrn=_assertion_urn(ds_urn, r["check"], r["column"]), aspect=info
            )
        )
    return mcps


def _ledger_rows(dataset_id: str) -> list[tuple]:
    # Inside Airflow the warehouse is on its compose-internal port; on a laptop, the mapped one.
    port = os.environ.get("ANALYTICS_INTERNAL_PORT", os.environ.get("ANALYTICS_PORT", "5433"))
    conninfo = (
        f"host={os.environ.get('ANALYTICS_HOST', 'localhost')} port={port} dbname={DB} "
        f"user=platform_loader password={os.environ['PLATFORM_LOADER_PASSWORD']}"
    )
    with psycopg.connect(conninfo) as conn:
        return conn.execute(
            "SELECT id, run_id, status, source_identifier, object_version, partition, archive_key,"
            " row_count, contract_version, checks, loaded_at FROM platform_meta.ingestion_ledger"
            " WHERE dataset = %s AND checks IS NOT NULL ORDER BY loaded_at",
            (dataset_id,),
        ).fetchall()


def _run_event_mcps(dataset_id: str, ds_urn: str) -> tuple[list, int]:
    mcps, files = [], 0
    for (lid, run_id, status, key, version, partition, archive_key, rows, cver, checks,
         loaded_at) in _ledger_rows(dataset_id):  # fmt: skip
        files += 1
        filed_to = f"s3://{ARCHIVE}/{archive_key}" if archive_key else ""
        reason = (
            filed_to.rsplit("/", 1)[0] + f"/{layout.REASON_FILE}" if status == "quarantined" else ""
        )
        for c in checks:
            native = {
                "detail": c["detail"], "file": f"s3://{INBOX}/{key}",
                "object_version": version or "", "outcome": status, "filed_to": filed_to,
                "contract_version": cver or "",
                "ledger_id": str(lid),
            }  # fmt: skip
            if reason:
                native["reason"] = reason
            mcps.append(
                MetadataChangeProposalWrapper(
                    entityUrn=_assertion_urn(ds_urn, c["check"], c["column"]),
                    aspect=AssertionRunEventClass(
                        timestampMillis=int(loaded_at.astimezone(UTC).timestamp() * 1000),
                        runId=f"{run_id}#{lid}",
                        asserteeUrn=ds_urn,
                        assertionUrn=_assertion_urn(ds_urn, c["check"], c["column"]),
                        status=AssertionRunStatusClass.COMPLETE,
                        partitionSpec=PartitionSpecClass(
                            partition=partition or "", type=PartitionTypeClass.PARTITION
                        ),
                        result=AssertionResultClass(
                            type=AssertionResultTypeClass.SUCCESS
                            if c["passed"]
                            else AssertionResultTypeClass.FAILURE,
                            rowCount=rows,
                            nativeResults=native,
                        ),
                    ),
                )
            )
    return mcps, files


def _stale_s3_datasets(current: set[str]) -> list[str]:
    """S3 datasets in the catalog that are not current landing datasets.

    The platform filter is a hint only: GMS search may ignore it, so the result is re-checked
    by URN before anything is soft-deleted (the landing zone owns the s3 platform, nothing else).
    """
    s3 = "urn:li:dataset:(urn:li:dataPlatform:s3,"
    body = json.dumps(
        {"input": "*", "entity": "dataset", "start": 0, "count": 1000,
         "filter": {"or": [{"and": [{"field": "platform",
                                     "value": "urn:li:dataPlatform:s3"}]}]}}
    ).encode()  # fmt: skip
    req = urllib.request.Request(
        f"{GMS}/entities?action=search", data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req) as resp:  # noqa: S310 (local GMS)
        found = [e["entity"] for e in json.load(resp)["value"]["entities"]]
    return [u for u in found if u.startswith(s3) and u not in current]


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish landing datasets + gate results")
    parser.add_argument("--domain", help="only this domain's landing datasets (Airflow task)")
    args = parser.parse_args()
    emitter = DatahubRestEmitter(GMS)
    current: set[str] = set()
    total_events = 0
    for d in _landing_datasets():
        if args.domain and d["domain"] != args.domain:
            continue
        contract = load_contract(contract_path(REPO_ROOT, d["domain"], d["name"]))
        rules = LandingRules.from_contract(contract)
        ds_urn = make_dataset_urn("s3", layout.catalog_name(ARCHIVE, d["source"], d["name"]))
        current.add(ds_urn)
        dataset_id = f"{d['source']}/{d['name']}"
        events, files = _run_event_mcps(dataset_id, ds_urn)
        assertions = _assertion_mcps(contract, rules, ds_urn)
        for mcp in _dataset_mcps(d, contract, rules, ds_urn) + assertions + events:
            emitter.emit_mcp(mcp)
        total_events += len(events)
        print(
            f"  {dataset_id}: dataset + {len(assertions)} assertions + {len(events)} run events "
            f"({files} deliveries) -> {ds_urn}"
        )
    # Reconcile only on a full run: a domain-scoped run does not know the other domains' datasets.
    for urn in [] if args.domain else _stale_s3_datasets(current):
        emitter.emit_mcp(MetadataChangeProposalWrapper(entityUrn=urn, aspect=StatusClass(True)))
        print(f"  soft-deleted stale landing entity {urn}")
    print(f"Landing quality published: {len(current)} datasets, {total_events} run events.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
