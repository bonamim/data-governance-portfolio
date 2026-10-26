"""The mesh's governed definitions, read in one place.

Everything a check, a generator or an agent needs to know about the mesh comes from files in git:
the domain registry, the governed tags and the ontology (``governance/``), and each domain's data
products and contracts (``domains/<domain>/``). This package reads them, so no tool hardcodes a
domain, a tag or a path:

- ``repo``      where the repository is, and how its YAML is read
- ``registry``  domains, governed tags, the ontology and its value types
- ``products``  data products and the output ports they publish
- ``approvals`` consumers' sign-off on a breaking change or a retirement
"""

__all__ = ["__version__"]
__version__ = "0.1.0"
