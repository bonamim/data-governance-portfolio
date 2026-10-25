"""Platform DAG bundle: acquire the shared Olist dataset once for all domains."""

from dgp_platform_orchestration.ingestion_dag import build_acquire_dag

dag = build_acquire_dag()
