"""Platform DAG bundle: PoC simulator of the source systems (not deployed in production)."""

from dgp_platform_orchestration.ingestion_dag import build_simulator_dag

dag = build_simulator_dag()
