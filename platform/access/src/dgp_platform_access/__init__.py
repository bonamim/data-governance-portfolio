"""Access: who may read what (ADR-0013, ADR-0024).

Access is declared in the governed definitions and enforced by the platform:

- ``apply_grants``             consumers get exactly the output ports their data products declare
- ``apply_masking``            PII is masked by role-aware views, per the contracts' policies
- ``generate_landing_access``  the landing zone's object-store policies, from each sources.yml
"""

__all__ = ["__version__"]
__version__ = "0.1.0"
