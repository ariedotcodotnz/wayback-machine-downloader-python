from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

from ..config import DownloadConfig
from ..models import Snapshot
from ..paths import LocalPathMapper
from ..requisites import PageRequisitesExtractor
from ..text import decode_best_effort


@dataclass(slots=True)
class AssetSnapshotIndex:
    """In-memory lookup for choosing archived asset timestamps."""

    timestamps_by_url: dict[str, list[int]]

    @classmethod
    def from_raw_snapshots(cls, raw_snapshots: list[tuple[int, str]]) -> AssetSnapshotIndex:
        index: dict[str, list[int]] = {}
        for timestamp, url in raw_snapshots:
            index.setdefault(cls.key(url), []).append(int(timestamp))
        return cls(index)

    def resolve(self, asset_url: str, parent_timestamp: int) -> int:
        timestamps = self.timestamps_by_url.get(self.key(asset_url))
        if not timestamps:
            return parent_timestamp

        eligible = [timestamp for timestamp in timestamps if timestamp <= parent_timestamp]
        return max(eligible) if eligible else max(timestamps)

    @staticmethod
    def key(url: str) -> str:
        """Build a scheme-insensitive lookup key from host, path, and query."""

        parsed = urlsplit(url if "://" in url else f"http://{url}")
        host = (parsed.hostname or "").lower()
        path = parsed.path or "/"
        return f"{host}{path}?{parsed.query}" if parsed.query else f"{host}{path}"


@dataclass(slots=True)
class AssetSnapshotPlanner:
    """Convert discovered page assets and rewritten URLs into download jobs."""

    config: DownloadConfig
    mapper: LocalPathMapper
    snapshot_index: AssetSnapshotIndex | None = None

    HTML_SUFFIXES = {".html", ".htm", ".php", ".asp", ".aspx", ".jsp"}
    REQUISITE_SKIP_SUFFIXES = {"", ".html", ".htm", ".php", ".asp", ".aspx", ".jsp"}
    _WAYBACK_EMBED_RE = re.compile(r"^/web/([0-9]{4,})[^/]*/(https?://.+)$")

    def discover_from_html(self, file_path: Path, parent_snapshot: Snapshot) -> list[Snapshot]:
        """Read a saved HTML page and return downloadable asset snapshots."""

        content = decode_best_effort(file_path.read_bytes())
        parent_url = self._absolute_parent_url(parent_snapshot.original_url)

        snapshots: list[Snapshot] = []
        for asset_reference in PageRequisitesExtractor.extract(content):
            normalized = self._normalize_page_reference(asset_reference, parent_url, parent_snapshot.timestamp)
            if normalized is None:
                continue
            asset_url, hint_timestamp = normalized
            snapshot = self.snapshot_for_url(asset_url, hint_timestamp)
            if snapshot is not None:
                snapshots.append(snapshot)
        return snapshots

    def snapshot_for_url(self, asset_url: str, hint_timestamp: int) -> Snapshot | None:
        """Create a download snapshot for an absolute asset URL."""

        parsed = urlsplit(asset_url)
        if not parsed.scheme or not parsed.hostname:
            return None

        target_host = self.target_host()
        if target_host is None:
            return None
        if not self.config.cross_host and parsed.hostname != target_host:
            return None

        normalized_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
        if parsed.hostname == target_host:
            asset_file_id = parsed.path.lstrip("/")
            if parsed.query:
                asset_file_id = f"{asset_file_id}?{parsed.query}"
        else:
            asset_file_id = normalized_url

        timestamp = self.resolve_timestamp(normalized_url, hint_timestamp)
        return Snapshot(
            original_url=normalized_url,
            timestamp=timestamp,
            file_id=self.mapper.sanitize_file_id(asset_file_id, normalized_url),
        )

    def resolve_timestamp(self, asset_url: str, parent_timestamp: int) -> int:
        if self.snapshot_index is None:
            return parent_timestamp
        return self.snapshot_index.resolve(asset_url, parent_timestamp)

    def target_host(self) -> str | None:
        candidate = self.config.target if "://" in self.config.target else f"https://{self.config.target}"
        return urlsplit(candidate).hostname

    def _normalize_page_reference(
        self,
        asset_reference: str,
        parent_url: str,
        hint_timestamp: int,
    ) -> tuple[str, int] | None:
        try:
            resolved = urljoin(parent_url, asset_reference)
            parsed = urlsplit(resolved)

            wayback_match = self._WAYBACK_EMBED_RE.match(parsed.path)
            if wayback_match:
                hint_timestamp = int(wayback_match.group(1))
                parsed = urlsplit(wayback_match.group(2))

            if Path(parsed.path).suffix.lower() in self.REQUISITE_SKIP_SUFFIXES:
                return None

            asset_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
            return asset_url, hint_timestamp
        except Exception:
            return None

    @staticmethod
    def _absolute_parent_url(parent_url: str) -> str:
        return parent_url if "://" in parent_url else f"http://{parent_url}"
