"""Customer Experience domain DAG bundle. All a domain writes is this factory call (ADR-0008)."""

from dgp_platform_orchestration.ingestion_dag import build_ingestion_dag

dag = build_ingestion_dag("customer_experience")
