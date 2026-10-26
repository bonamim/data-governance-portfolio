"""The catalog: DataHub as the read model of the mesh's governance (ADR-0018).

Technical metadata — datasets, lineage, dbt tests — comes from DataHub's own ingestion recipes
(``recipes/``). The governance on top of it is generated here from the definitions in git, never
typed into the UI:

- ``publish_governance``       domains, data products, the business glossary, governed tags, and
                               which DAG builds each dataset
- ``publish_landing_quality``  the landing datasets and every contract-gate result, per file
- ``gms``                      a small read client for DataHub's metadata service
"""

__all__ = ["__version__"]
__version__ = "0.1.0"
