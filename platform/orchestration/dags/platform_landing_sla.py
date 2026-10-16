"""Platform DAG bundle: landing delivery deadlines -> alerts to the owning domain (ADR-0016)."""

from dgp_platform_orchestration.ingestion_dag import build_landing_sla_dag

dag = build_landing_sla_dag()
