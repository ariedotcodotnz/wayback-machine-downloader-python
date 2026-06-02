from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from .filters import URLFilter
from .models import Snapshot
from .paths import LocalPathMapper


@dataclass(slots=True)
class SnapshotPlanner:
    filters: URLFilter
    mapper: LocalPathMapper

    def build(
        self,
        raw_snapshots: list[tuple[int, str]],
        *,
        all_timestamps: bool,
        snapshot_at: int | None,
    ) -> list[Snapshot]:
        """Choose the snapshot selection mode requested by the CLI."""

        if snapshot_at is not None:
            return self._composite_snapshot(raw_snapshots, snapshot_at)
        if all_timestamps:
            return self._all_timestamps(raw_snapshots)
        return self._latest_per_file(raw_snapshots)

    def _latest_per_file(self, raw_snapshots: list[tuple[int, str]]) -> list[Snapshot]:
        # Default mode keeps only the newest capture for each logical file.
        curated: dict[str, Snapshot] = {}
        for timestamp, file_url in raw_snapshots:
            candidate = self._snapshot_from_record(timestamp, file_url)
            if candidate is None:
                continue
            existing = curated.get(candidate.file_id)
            if existing is None or existing.timestamp <= candidate.timestamp:
                curated[candidate.file_id] = candidate
        return sorted(curated.values(), key=lambda item: item.timestamp, reverse=True)

    def _all_timestamps(self, raw_snapshots: list[tuple[int, str]]) -> list[Snapshot]:
        # In all-timestamps mode the timestamp becomes part of the logical ID.
        curated: dict[str, Snapshot] = {}
        for timestamp, file_url in raw_snapshots:
            base_candidate = self._snapshot_from_record(timestamp, file_url)
            if base_candidate is None:
                continue
            combined_id = self.mapper.sanitize_file_id(f"{timestamp}/{base_candidate.file_id}", file_url)
            curated.setdefault(
                combined_id,
                Snapshot(file_url, int(timestamp), combined_id),
            )
        return sorted(curated.values(), key=lambda item: item.timestamp, reverse=True)

    def _composite_snapshot(self, raw_snapshots: list[tuple[int, str]], target_timestamp: int) -> list[Snapshot]:
        # Composite mode picks the newest file version at or before the target.
        file_versions: dict[str, Snapshot] = {}
        for timestamp, file_url in raw_snapshots:
            if int(timestamp) > target_timestamp:
                continue
            candidate = self._snapshot_from_record(timestamp, file_url)
            if candidate is None:
                continue
            existing = file_versions.get(candidate.file_id)
            if existing is None or existing.timestamp < candidate.timestamp:
                file_versions[candidate.file_id] = candidate
        return sorted(file_versions.values(), key=lambda item: item.timestamp, reverse=True)

    def _snapshot_from_record(self, timestamp: int, file_url: str) -> Snapshot | None:
        if not self.filters.allows(file_url):
            return None
        file_id = self.mapper.sanitize_file_id(self._raw_tail(file_url), file_url)
        return Snapshot(file_url, int(timestamp), file_id)

    @staticmethod
    def _raw_tail(file_url: str) -> str:
        """Return the path/query part used as the logical file identifier."""

        parsed = urlsplit(file_url if "://" in file_url else f"http://{file_url}")
        path = parsed.path.lstrip("/")
        if parsed.query:
            return f"{path}?{parsed.query}"
        return path
