"""Self-serve ingestion for the data mesh platform.

Platform code only. Domains contribute declarative source config
(``domains/<domain>/ingestion/sources.yml``) and never import from here directly;
they call this capability through the orchestrator (ADR-0008).
"""

__all__ = ["__version__"]
__version__ = "0.1.0"
