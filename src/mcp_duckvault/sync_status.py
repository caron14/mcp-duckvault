"""Structured indexing and synchronization outcomes."""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class IndexStatus(str, Enum):
    INDEXED = "indexed"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class IndexResult:
    path: str
    status: IndexStatus
    error_code: str | None = None
    error_type: str | None = None
    occurred_at: datetime | None = None

    def as_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["status"] = self.status.value
        if self.occurred_at:
            data["occurred_at"] = self.occurred_at.isoformat()
        return data


@dataclass
class SyncSummary:
    scanned: int = 0
    indexed: int = 0
    skipped: int = 0
    deleted: int = 0
    failed: int = 0
    excluded: int = 0
    started_at: datetime = field(default_factory=utc_now)
    finished_at: datetime | None = None
    failures: list[IndexResult] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.failed == 0:
            return "complete"
        if self.indexed or self.skipped or self.deleted:
            return "partial"
        return "failed"

    def finish(self) -> "SyncSummary":
        self.finished_at = utc_now()
        return self

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "scanned": self.scanned,
            "indexed": self.indexed,
            "skipped": self.skipped,
            "deleted": self.deleted,
            "failed": self.failed,
            "excluded": self.excluded,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "failures": [failure.as_dict() for failure in self.failures],
        }


@dataclass
class SyncPlan:
    """Read-only preview of one full synchronization."""

    scanned: int = 0
    indexed: int = 0
    skipped: int = 0
    deleted: int = 0
    failed: int = 0
    excluded: int = 0
    total_bytes: int = 0
    paths: dict[str, list[str]] = field(
        default_factory=lambda: {
            "indexed": [],
            "skipped": [],
            "deleted": [],
            "failed": [],
            "excluded": [],
        }
    )
    reasons: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "status": "planned",
            "scanned": self.scanned,
            "indexed": self.indexed,
            "skipped": self.skipped,
            "deleted": self.deleted,
            "failed": self.failed,
            "excluded": self.excluded,
            "total_bytes": self.total_bytes,
            "paths": self.paths,
            "reasons": self.reasons,
        }
