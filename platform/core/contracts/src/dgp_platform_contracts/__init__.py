"""ODCS contracts for the data mesh platform.

The one place a contract is read (``odcs.load``): linked definitions are inlined, so every
reader (access, quality, context and the core) sees the same resolved contract (ADR-0027).
Pure pyyaml, so the lightweight runtimes can import it. ADR-0008 boundaries apply.
"""

__all__ = ["__version__"]
__version__ = "0.1.0"
