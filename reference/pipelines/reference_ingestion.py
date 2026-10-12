"""Reference data DAG bundle (platform-owned shared data). Factory call only."""

from dgp_platform_orchestration.ingestion_dag import build_ingestion_dag

dag = build_ingestion_dag("reference")
