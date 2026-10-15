"""Landing-zone layout: where a delivery is filed, and under which rules (ADR-0024).

Two buckets, keyed by lifecycle stage so one lifecycle rule or policy covers every source:

    <inbox>/<source>/<dataset>/<whatever the partner sends>
    <archive>/accepted/<source>/<dataset>/<partition>/<file>
    <archive>/quarantine/<source>/<dataset>/<partition>/<run_id>/<file>  (+ _reason.json)

The partner's path is free-form under its prefix. The partition is ours, derived from the
landing contract's ``customProperties`` (scheme, source, file-name pattern), never imposed on the
partner. Everything here is pure (no I/O) so it is unit-tested directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from dgp_platform_quality.contract import Contract

# ODCS partitionScheme value -> strftime format of the archive partition path.
PARTITION_SCHEMES = {
    "dt=YYYY-MM-DD": "dt=%Y-%m-%d",
    "dt=YYYY-MM-DD/hr=HH": "dt=%Y-%m-%d/hr=%H",
    "dt=YYYY-MM": "dt=%Y-%m",
}
PARTITION_SOURCES = ("filename", "arrival")
COMPLETION_SIGNALS = ("per_file", "settle")
# Accepted-file retention classes; each has exactly one archive lifecycle rule (minio-init).
RETENTION_CLASSES = (90, 365, 2555)

ACCEPTED = "accepted"
QUARANTINE = "quarantine"
REASON_FILE = "_reason.json"


class FilenameMismatch(ValueError):
    """The delivered file name does not follow the contract's ``filenamePattern``."""


@dataclass(frozen=True)
class LandingRules:
    """A landing dataset's operational rules, read from its ODCS contract."""

    partition_scheme: str = "dt=YYYY-MM-DD"
    partition_source: str = "arrival"
    filename_pattern: re.Pattern | None = None
    retention_days: int = 365
    completion_signal: str = "per_file"
    settle_minutes: int = 0

    @classmethod
    def from_contract(cls, contract: Contract | None) -> LandingRules:
        if contract is None:
            return cls()
        p = contract.properties
        pattern = p.get("filenamePattern")
        return cls(
            partition_scheme=str(p.get("partitionScheme", cls.partition_scheme)),
            partition_source=str(p.get("partitionSource", "filename" if pattern else "arrival")),
            filename_pattern=re.compile(pattern) if pattern else None,
            retention_days=int(p.get("retentionDays", cls.retention_days)),
            completion_signal=str(p.get("completionSignal", cls.completion_signal)),
            settle_minutes=int(p.get("settleMinutes", 0)),
        )

    def problems(self) -> list[str]:
        """Why these rules cannot be executed (empty when valid). Used by CI and the loader."""
        out = []
        if self.partition_scheme not in PARTITION_SCHEMES:
            out.append(f"partitionScheme '{self.partition_scheme}' not in {[*PARTITION_SCHEMES]}")
        if self.partition_source not in PARTITION_SOURCES:
            out.append(f"partitionSource '{self.partition_source}' not in {PARTITION_SOURCES}")
        if self.partition_source == "filename":
            if self.filename_pattern is None:
                out.append("partitionSource 'filename' needs a filenamePattern")
            else:
                groups = set(self.filename_pattern.groupindex)
                if "date" not in groups and not {"year", "month"} <= groups:
                    out.append("filenamePattern needs a (?P<date>) or (?P<year>)(?P<month>) group")
                hourly = "hr=" in self.partition_scheme
                if hourly and "hour" not in groups:
                    out.append("an hourly partitionScheme needs a (?P<hour>) group")
        if self.retention_days not in RETENTION_CLASSES:
            out.append(f"retentionDays {self.retention_days} not in {RETENTION_CLASSES}")
        if self.completion_signal not in COMPLETION_SIGNALS:
            out.append(
                f"completionSignal '{self.completion_signal}' not supported yet "
                f"{COMPLETION_SIGNALS} (expected_files/marker come with ADR-0016)"
            )
        return out

    def is_settled(self, last_modified: datetime, now: datetime) -> bool:
        """True when the object may be processed under the contract's completion signal."""
        if self.completion_signal != "settle":
            return True
        return now - last_modified >= timedelta(minutes=self.settle_minutes)

    def partition_for(self, filename: str, arrived: datetime) -> str:
        """The archive partition for a delivered file (raises FilenameMismatch)."""
        fmt = PARTITION_SCHEMES[self.partition_scheme]
        if self.partition_source == "arrival":
            return arrived.astimezone(UTC).strftime(fmt)
        assert self.filename_pattern is not None
        m = self.filename_pattern.match(filename)
        if not m:
            raise FilenameMismatch(
                f"'{filename}' does not match filenamePattern {self.filename_pattern.pattern!r}"
            )
        return _business_time(m.groupdict()).strftime(fmt)

    def partition_from_arrival(self, arrived: datetime) -> str:
        """Fallback partition (used to file a rejected file whose name could not be parsed)."""
        return arrived.astimezone(UTC).strftime(PARTITION_SCHEMES[self.partition_scheme])

    def retain_until(self, archived: datetime) -> datetime:
        return archived + timedelta(days=self.retention_days)

    @property
    def retention_tag(self) -> str:
        return f"retention-class={self.retention_days}d"


def _business_time(groups: dict[str, str | None]) -> datetime:
    if groups.get("date"):
        digits = groups["date"].replace("-", "")
        base = datetime.strptime(digits[:8] if len(digits) >= 8 else digits + "01", "%Y%m%d")
    else:
        base = datetime(int(groups["year"]), int(groups["month"]), int(groups.get("day") or 1))
    return base.replace(hour=int(groups.get("hour") or 0), tzinfo=UTC)


def inbox_prefix(source: str, dataset: str) -> str:
    return f"{source}/{dataset}/"


def accepted_key(source: str, dataset: str, partition: str, filename: str) -> str:
    return f"{ACCEPTED}/{source}/{dataset}/{partition}/{filename}"


def accepted_prefix(source: str, dataset: str, partition: str = "") -> str:
    return f"{ACCEPTED}/{source}/{dataset}/" + (f"{partition}/" if partition else "")


def quarantine_key(source: str, dataset: str, partition: str, run_id: str, filename: str) -> str:
    return f"{QUARANTINE}/{source}/{dataset}/{partition}/{_safe(run_id)}/{filename}"


def catalog_name(archive_bucket: str, source: str, dataset: str) -> str:
    """The landing dataset's name in the catalog (DataHub s3 platform), shared by Airflow lineage
    and the quality publisher so both land on the same entity."""
    return f"{archive_bucket}/{ACCEPTED}/{source}/{dataset}"


def _safe(run_id: str) -> str:
    # Airflow run ids contain ':' and '+'; keep keys portable across object stores and shells.
    return re.sub(r"[^A-Za-z0-9._=-]", "_", run_id)
