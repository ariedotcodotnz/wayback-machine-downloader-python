from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Mapping


@dataclass(slots=True, frozen=True)
class Snapshot:
    original_url: str
    timestamp: int
    file_id: str


class FetchDisposition(str, Enum):
    SAVED = "saved"
    NOT_FOUND = "not_found"


@dataclass(slots=True, frozen=True)
class HTTPResponse:
    status: int
    reason: str
    headers: Mapping[str, str]
    body: bytes


@dataclass(slots=True, frozen=True)
class FetchResult:
    disposition: FetchDisposition
    body: bytes | None
    source_url: str
    status_code: int


@dataclass(slots=True, frozen=True)
class FailedDownload:
    url: str
    error: str


@dataclass(slots=True)
class DownloadSummary:
    root: Path
    discovered: int
    queued: int
    completed: int
    skipped_existing: int
    failures: list[FailedDownload] = field(default_factory=list)
