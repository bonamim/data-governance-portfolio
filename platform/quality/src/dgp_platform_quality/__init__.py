"""Quality engine for the data mesh platform.

Executes the quality rules declared in an ODCS contract against a dataset. The
engine knows nothing dataset-specific: the rules come from the contract (the
domain), the mandate from governance, and the platform only runs them
(data-mesh.md, "Where quality lives"). ADR-0008 boundaries apply.
"""

__all__ = ["__version__"]
__version__ = "0.1.0"
