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
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta

from dgp_platform_quality.rules import Contract

# ODCS partitionScheme value -> strftime format of the archive partition path.
PARTITION_SCHEMES = {
    "dt=YYYY-MM-DD": "dt=%Y-%m-%d",
    "dt=YYYY-MM-DD/hr=HH": "dt=%Y-%m-%d/hr=%H",
    "dt=YYYY-MM": "dt=%Y-%m",
}
PARTITION_SOURCES = ("filename", "arrival")
# When a delivery may be processed (ADR-0016):
#   per_file        every file on arrival
#   settle          a file once nothing has changed it for settleMinutes
#   expected_files  a partition once expectedFiles data files for it are in the inbox
#   marker          a partition once the partner's completion marker for it is in the inbox
COMPLETION_SIGNALS = ("per_file", "settle", "expected_files", "marker")
BATCH_SIGNALS = ("expected_files", "marker")
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
    expected_files: int | None = None
    marker_pattern: re.Pattern | None = None
    # Delivery deadline (ODCS slaProperties.timeOfAvailability, UTC) and how many days the
    # business date trails it (slaProperties.latency in days): by 04:00 on day D, dt=D-1 is due.
    time_of_availability: time | None = None
    business_date_lag_days: int = 1

    @classmethod
    def from_contract(cls, contract: Contract | None) -> LandingRules:
        if contract is None:
            return cls()
        p = contract.properties
        pattern = p.get("filenamePattern")
        marker = p.get("completionMarker")
        toa = (contract.sla.get("timeOfAvailability") or {}).get("value")
        latency = contract.sla.get("latency") or {}
        lag = (
            int(latency.get("value", 1)) if latency.get("unit", "d") in ("d", "day", "days") else 1
        )
        return cls(
            partition_scheme=str(p.get("partitionScheme", cls.partition_scheme)),
            partition_source=str(p.get("partitionSource", "filename" if pattern else "arrival")),
            filename_pattern=re.compile(pattern) if pattern else None,
            retention_days=int(p.get("retentionDays", cls.retention_days)),
            completion_signal=str(p.get("completionSignal", cls.completion_signal)),
            settle_minutes=int(p.get("settleMinutes", 0)),
            expected_files=int(p["expectedFiles"]) if p.get("expectedFiles") else None,
            marker_pattern=re.compile(marker) if marker else None,
            time_of_availability=_parse_toa(toa) if toa else None,
            business_date_lag_days=lag,
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
            out.append(f"completionSignal '{self.completion_signal}' not in {COMPLETION_SIGNALS}")
        if self.completion_signal == "expected_files" and not (self.expected_files or 0) >= 1:
            out.append("completionSignal 'expected_files' needs expectedFiles >= 1")
        if self.completion_signal == "marker":
            if self.marker_pattern is None:
                out.append("completionSignal 'marker' needs a completionMarker pattern")
            elif self.partition_source == "filename":
                groups = set(self.marker_pattern.groupindex)
                if "date" not in groups and not {"year", "month"} <= groups:
                    out.append("completionMarker needs the partition's (?P<date>) group")
        return out

    @property
    def batched(self) -> bool:
        return self.completion_signal in BATCH_SIGNALS

    def is_marker(self, filename: str) -> bool:
        return bool(self.marker_pattern and self.marker_pattern.match(filename))

    def marker_partition(self, filename: str, arrived: datetime) -> str:
        """The partition a completion marker closes (same date groups as the data files)."""
        fmt = PARTITION_SCHEMES[self.partition_scheme]
        if self.partition_source == "arrival":
            return arrived.astimezone(UTC).strftime(fmt)
        assert self.marker_pattern is not None
        m = self.marker_pattern.match(filename)
        assert m is not None
        return _business_time(m.groupdict()).strftime(fmt)

    def due_partition(self, now: datetime) -> tuple[str, datetime] | None:
        """The partition that must be in by now, and its deadline (None: no deadline declared).

        A daily dataset with timeOfAvailability 04:00 and a one-day latency owes dt=D-1 by 04:00
        on day D; before that deadline, the previous day's partition is the one checked.
        """
        if self.time_of_availability is None:
            return None
        now = now.astimezone(UTC)
        deadline = datetime.combine(now.date(), self.time_of_availability, tzinfo=UTC)
        if now < deadline:
            deadline -= timedelta(days=1)
        business = deadline - timedelta(days=self.business_date_lag_days)
        return business.strftime(PARTITION_SCHEMES[self.partition_scheme]), deadline

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

    def in_future(self, partition: str, now: datetime) -> bool:
        """Does a file-name partition start after tomorrow (UTC)? A business date from the future
        is a partner error; filed as delivered it would pose as the newest data."""
        m = re.match(r"dt=(\d{4})-(\d{2})(?:-(\d{2}))?", partition)
        if self.partition_source != "filename" or not m:
            return False
        start = datetime(int(m[1]), int(m[2]), int(m[3] or 1), tzinfo=UTC)
        return start > now.astimezone(UTC) + timedelta(days=1)

    def retain_until(self, archived: datetime) -> datetime:
        return archived + timedelta(days=self.retention_days)

    @property
    def retention_tag(self) -> str:
        return f"retention-class={self.retention_days}d"


def _parse_toa(value: str) -> time:
    """'04:00+00:00' / '04:00' -> 04:00 UTC (offsets other than UTC are converted)."""
    text = str(value).strip()
    parsed = datetime.fromisoformat(f"2000-01-01T{text}")
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC)
    return parsed.time().replace(tzinfo=None)


@dataclass
class Batch:
    """Files the gate decides on together: one file, or a whole partition (batch signals)."""

    partition: str | None
    files: list = field(default_factory=list)
    marker: object | None = None


def plan_batches(rules: LandingRules, objects: list, now: datetime) -> tuple[list, list]:
    """Split inbox objects (anything with ``filename`` and ``last_modified``) into ready batches
    and waiting ones, by the contract's completion signal.

    A file whose name can't be parsed is its own ready batch: the gate quarantines it by the
    naming rule rather than letting it hold up a partition.
    """
    if not rules.batched:
        ready = [o for o in objects if rules.is_settled(o.last_modified, now)]
        waiting = [Batch(None, [o]) for o in objects if o not in ready]
        return [Batch(None, [o]) for o in ready], waiting
    groups: dict[str, Batch] = {}
    ready: list[Batch] = []
    for o in objects:
        if rules.is_marker(o.filename):
            part = rules.marker_partition(o.filename, o.last_modified)
            groups.setdefault(part, Batch(part)).marker = o
            continue
        try:
            part = rules.partition_for(o.filename, o.last_modified)
        except FilenameMismatch:
            ready.append(Batch(None, [o]))
            continue
        groups.setdefault(part, Batch(part)).files.append(o)
    waiting = []
    for part in sorted(groups):
        b = groups[part]
        if rules.completion_signal == "expected_files":
            complete = len(b.files) >= (rules.expected_files or 1)
        else:
            complete = b.marker is not None and bool(b.files)
        (ready if complete else waiting).append(b)
    return ready, waiting


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


def accepted_partition(source: str, dataset: str, key: str) -> str:
    """The partition of an accepted key, e.g. ``dt=2026-10-15`` or ``dt=2026-10-15/hr=07``."""
    return key[len(accepted_prefix(source, dataset)) :].rsplit("/", 1)[0]


def newest_as_of(source: str, dataset: str, keys: list[str], date: str) -> str | None:
    """The accepted key in the newest partition on or before ``date`` (YYYY-MM-DD), or None.

    Datasets deliver on their own cadence, so "the state as of a date" is each dataset's latest
    accepted file up to then, not a file for exactly that date. Within one partition the last key
    wins (``keys`` arrive oldest first, as ``ObjectStore.list`` returns them).
    """
    candidates = [
        (part, i, key)
        for i, key in enumerate(keys)
        if (m := re.match(r"dt=([\d-]+)", part := accepted_partition(source, dataset, key)))
        and m.group(1) <= date
    ]
    return max(candidates)[2] if candidates else None


def quarantine_key(source: str, dataset: str, partition: str, run_id: str, filename: str) -> str:
    return f"{QUARANTINE}/{source}/{dataset}/{partition}/{_safe(run_id)}/{filename}"


def catalog_name(archive_bucket: str, source: str, dataset: str) -> str:
    """The landing dataset's name in the catalog (DataHub s3 platform), shared by Airflow lineage
    and the quality publisher so both land on the same entity."""
    return f"{archive_bucket}/{ACCEPTED}/{source}/{dataset}"


def _safe(run_id: str) -> str:
    # Airflow run ids contain ':' and '+'; keep keys portable across object stores and shells.
    return re.sub(r"[^A-Za-z0-9._=-]", "_", run_id)
