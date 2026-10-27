"""The semantic layer: Apache Ossie models, read and compiled in one place (ADR-0010, ADR-0027).

``ossie`` is the reader every consumer shares (the BI publisher, the AI agents, the bundle build
and the governance checks), so a metric means the same thing, and compiles to the same SQL,
wherever it is asked for. Ossie's own validator and schemas are vendored, unchanged, in
``vendor/ossie/``.
"""

__all__ = ["__version__"]
__version__ = "0.1.0"
