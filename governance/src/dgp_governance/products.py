"""Data products and the output ports they publish.

A data product is declared by its definition, ``domains/<domain>/data_products/<name>.yaml``
(purpose, owner, consumers, inputs: governance/standards/data-product.md). Its output port is the
contract that publishes it, ``domains/<domain>/contracts/<name>.odcs.yaml``, tagged
``output_port`` (ADR-0012), and the dbt model of the same name builds it. The port's physical home
follows from the medallion folder its model lives in: ``<domain>_<layer>.<name>``.

These records are what the checks, the access tools, the catalog and the agents share, so "who
reads this port" or "where does it live" has one answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from dgp_platform_contracts import odcs

from .repo import domain_of, load_yaml, root


@dataclass(frozen=True)
class DataProduct:
    """A data product definition, as its domain wrote it."""

    name: str
    domain: str
    path: Path
    definition: dict = field(repr=False)

    @classmethod
    def load(cls, path: Path) -> DataProduct:
        definition = load_yaml(path)
        return cls(definition.get("name", path.stem), domain_of(path), path, definition)

    @property
    def owner(self) -> str | None:
        """The owning team (``owner.team``)."""
        return (self.definition.get("owner") or {}).get("team")

    @property
    def consumers(self) -> list[str]:
        """The teams declared as consumers."""
        return [c["team"] for c in self.definition.get("consumers") or [] if (c or {}).get("team")]

    @property
    def inputs(self) -> list[str]:
        """The output ports it consumes, as ``<domain>.<port>``."""
        return [str(i) for i in self.definition.get("inputs") or []]

    @property
    def version_of(self) -> str | None:
        """The port this one is a new major version of (ADR-0019 §3), if any."""
        return self.definition.get("version_of")

    @property
    def qualified_name(self) -> str:
        return f"{self.domain}.{self.name}"


@dataclass(frozen=True)
class OutputPort:
    """An output-port contract, resolved: every column carries what it inherits from its
    definition (``dgp_platform_contracts.odcs``)."""

    name: str
    domain: str
    contract: Path
    document: dict = field(repr=False)
    schema: dict = field(repr=False)

    @classmethod
    def load(cls, contract: Path) -> OutputPort | None:
        """The port a contract publishes, or None if the contract is not an output port."""
        document = odcs.load(contract)
        schema = odcs.output_port(document)
        if schema is None:
            return None
        name = schema.get("name", contract.name.removesuffix(".odcs.yaml"))
        return cls(name, domain_of(contract), contract, document, schema)

    @property
    def columns(self) -> list[dict]:
        """The port's columns (resolved ODCS properties), in contract order."""
        return [p for p in self.schema.get("properties") or [] if p.get("name")]

    @property
    def column_names(self) -> set[str]:
        return {p["name"] for p in self.columns}

    @property
    def version(self) -> str:
        return str(self.document.get("version", "0"))

    @property
    def major(self) -> int:
        head = self.version.split(".")[0]
        return int(head) if head.isdigit() else 0

    @property
    def status(self) -> str:
        return str(self.document.get("status", "active"))

    @property
    def sunset(self) -> date | None:
        """The end of a deprecated version's migration window (``sunsetDate``, ADR-0019 §3)."""
        value = odcs.custom_properties(self.document).get("sunsetDate")
        return date.fromisoformat(str(value)) if value else None

    def masking_policies(self) -> dict[str, dict]:
        """Column -> its ``maskingPolicy`` (ADR-0013), for every column that declares one."""
        out = {}
        for prop in self.columns:
            custom = odcs.custom_properties(prop)
            if "maskingPolicy" in custom:
                out[prop["name"]] = custom["maskingPolicy"] or {}
        return out

    @property
    def model_file(self) -> Path | None:
        return model_file(self.domain, self.name)

    @property
    def location(self) -> tuple[str, str] | None:
        return location(self.domain, self.name)


def data_products(domain: str | None = None) -> list[DataProduct]:
    """Every data product definition (only ``domain``'s when given), sorted by path."""
    pattern = f"domains/{domain or '*'}/data_products/*.yaml"
    return [DataProduct.load(p) for p in sorted(root().glob(pattern))]


def data_product(name: str) -> DataProduct | None:
    return next((p for p in data_products() if p.name == name), None)


def contract_files(domain: str | None = None) -> list[Path]:
    """Every output-port-level contract file (``domains/<d>/contracts/*.odcs.yaml``): landing
    contracts (``contracts/landing/``) and definitions (``contracts/definitions/``) sit deeper."""
    return sorted(root().glob(f"domains/{domain or '*'}/contracts/*.odcs.yaml"))


def output_ports(domain: str | None = None) -> dict[str, OutputPort]:
    """Port name -> output port, for every contract tagged ``output_port``."""
    ports = (OutputPort.load(c) for c in contract_files(domain))
    return {p.name: p for p in ports if p is not None}


def output_port(name: str, domain: str | None = None) -> OutputPort | None:
    """The output port called ``name`` (its contract is named after it), or None."""
    for contract in sorted(root().glob(f"domains/{domain or '*'}/contracts/{name}.odcs.yaml")):
        if (port := OutputPort.load(contract)) is not None:
            return port
    return None


def readers_of(domain: str, port: str) -> list[str]:
    """Data products that consume ``<domain>.<port>`` as an input (``<domain>.<product>``)."""
    wanted = f"{domain}.{port}"
    return [p.qualified_name for p in data_products() if wanted in p.inputs]


def affected_teams(port: str) -> set[str]:
    """Teams a change to this port affects: its declared consumers, plus the owner of every data
    product that reads it as an input."""
    teams: set[str] = set()
    for product in data_products():
        if product.name == port:
            teams.update(product.consumers)
        if product.owner and any(i.split(".")[-1] == port for i in product.inputs):
            teams.add(product.owner)
    return teams


def model_file(domain: str, product: str) -> Path | None:
    """The dbt model that builds ``product`` in ``domain``'s project, in any medallion layer."""
    return next((root() / "domains" / domain / "dbt" / "models").rglob(f"{product}.sql"), None)


def location(domain: str, product: str) -> tuple[str, str] | None:
    """Physical ``(schema, table)`` of a port: the schema is ``<domain>_<layer>``, the layer the
    folder its dbt model lives in (bronze | silver | gold, ADR-0012). No build needed."""
    model = model_file(domain, product)
    return (f"{domain}_{model.parent.name}", product) if model else None
