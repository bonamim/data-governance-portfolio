"""Discovery and validation of the domains' declarative source config.

Each domain registers its sources in ``<domain>/ingestion/sources.yml``. The
platform reads these *files* (data, not code, so the boundary in ADR-0008 holds),
so a domain onboards by adding config, never by changing platform code.

Example (domains/sales/ingestion/sources.yml):

    domain: sales
    datasets:
      - name: orders
        source: {type: oltp, olist_file: olist_orders_dataset.csv, oltp_table: orders}
        target: {raw_table: orders}
      - name: reviews
        source: {type: landing, olist_file: olist_order_reviews_dataset.csv,
                 delivered_as: 'olist_order_reviews_{date:%Y%m%d}.csv'}
        target: {raw_table: reviews}
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

SourceType = str  # "oltp" | "landing"


@dataclass(frozen=True)
class Dataset:
    domain: str  # the single owning domain (who is accountable for the contract)
    name: str
    source_type: SourceType
    olist_file: str
    raw_table: str
    oltp_table: str | None  # required when source_type == "oltp"
    source_system: str  # where the data comes from, which drives the landing path
    config_path: Path
    # PoC partner simulator only: the file name the partner delivers, as a format string over
    # the business date (e.g. 'reviews_{date:%Y%m%d}.csv'). Real partners name files their way,
    # and the landing contract describes that convention (filenamePattern, ADR-0024).
    delivered_as: str | None = None

    @property
    def raw_schema(self) -> str:
        return f"{self.domain}_raw"

    def delivered_name(self, business_date: datetime) -> str:
        """The file name the (simulated) partner uses for a delivery on ``business_date``."""
        if not self.delivered_as:
            return self.olist_file
        return self.delivered_as.format(date=business_date)


def _find_config_files(repo_root: Path) -> list[Path]:
    return sorted(repo_root.glob("domains/*/ingestion/sources.yml"))


def _parse_file(path: Path) -> list[Dataset]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    domain = raw.get("domain")
    if not domain:
        raise ValueError(f"{path}: missing 'domain'")
    source_system = raw.get("source_system", "olist")
    datasets: list[Dataset] = []
    for entry in raw.get("datasets", []):
        source = entry.get("source", {})
        target = entry.get("target", {})
        name = entry.get("name")
        stype = source.get("type")
        if stype not in ("oltp", "landing"):
            raise ValueError(f"{path}: dataset '{name}' has invalid source.type '{stype}'")
        if stype == "oltp" and not source.get("oltp_table"):
            raise ValueError(f"{path}: oltp dataset '{name}' needs source.oltp_table")
        datasets.append(
            Dataset(
                domain=domain,
                name=name,
                source_type=stype,
                olist_file=source["olist_file"],
                raw_table=target.get("raw_table", name),
                oltp_table=source.get("oltp_table"),
                source_system=source_system,
                config_path=path,
                delivered_as=source.get("delivered_as"),
            )
        )
    return datasets


def load_datasets(repo_root: Path, domain: str | None = None) -> list[Dataset]:
    """Load and validate domain datasets from the repo.

    With ``domain`` set, only that domain's datasets are returned. This is how a
    per-domain DAG stays scoped to its own data (domain isolation, proof P1).
    """
    datasets: list[Dataset] = []
    for path in _find_config_files(repo_root):
        datasets.extend(_parse_file(path))
    if not datasets:
        raise RuntimeError(f"no source config found under {repo_root}/domains/*/ingestion/")
    if domain is not None:
        datasets = [d for d in datasets if d.domain == domain]
        if not datasets:
            raise RuntimeError(f"no datasets found for domain '{domain}'")
    return datasets


def olist_files(datasets: list[Dataset]) -> set[str]:
    """The distinct Olist CSV files the datasets need (for acquisition)."""
    return {d.olist_file for d in datasets}
