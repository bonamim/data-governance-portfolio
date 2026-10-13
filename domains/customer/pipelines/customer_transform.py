"""Customer transform DAG bundle. All a domain writes is this factory call (ADR-0008)."""

from dgp_platform_orchestration.transform_dag import build_transform_dag

dag = build_transform_dag("customer")
