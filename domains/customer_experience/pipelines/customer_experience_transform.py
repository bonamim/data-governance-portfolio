"""Customer Experience transform DAG bundle. Factory call only (ADR-0008)."""

from dgp_platform_orchestration.transform_dag import build_transform_dag

dag = build_transform_dag("customer_experience")
