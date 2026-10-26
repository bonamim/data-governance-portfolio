"""Governed AI: agents that answer from the semantic layer, never around it (ADR-0020, ADR-0021).

- ``semantic``             the engine: open discovery, governed computation, requests to owners
- ``semantic_server``      one semantic layer as an MCP server (platform-wide, or a domain's agent)
- ``agents``               the agent registry, derived from the domains and their semantic models
- ``router``               deterministic routing of a question to the owning domain's agent
- ``orchestrator_server``  the router as a single MCP endpoint
"""

__all__ = ["__version__"]
__version__ = "0.1.0"
