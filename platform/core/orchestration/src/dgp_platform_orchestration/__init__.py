"""Orchestration capability for the data mesh platform.

Provides Airflow 3 DAG factories so each domain gets one DAG bundle that wires the
platform's ingestion without writing orchestration code (ADR-0005, ADR-0008).
"""

__all__ = ["__version__"]
__version__ = "0.1.0"
