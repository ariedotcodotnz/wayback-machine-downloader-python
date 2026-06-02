from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from .paths import LocalPathMapper, OutputLayout


class DownloadState:
    def __init__(self, layout: OutputLayout, logger: logging.Logger) -> None:
        self.layout = layout
        self.logger = logger
        self._db_lock = threading.Lock()

    def reset(self) -> None:
        """Delete cached CDX and download-state files for a fresh run."""

        self.layout.cdx_path.unlink(missing_ok=True)
        self.layout.db_path.unlink(missing_ok=True)

    def load_snapshot_cache(self) -> list[tuple[int, str]] | None:
        """Load a cached CDX listing if it is still readable."""

        if not self.layout.cdx_path.exists():
            return None
        try:
            payload = json.loads(self.layout.cdx_path.read_text(encoding="utf-8"))
            return [(int(timestamp), str(url)) for timestamp, url in payload]
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
            self.logger.warning("Ignoring corrupt snapshot cache %s: %s", self.layout.cdx_path, exc)
            self.layout.cdx_path.unlink(missing_ok=True)
            return None

    def save_snapshot_cache(self, snapshots: list[tuple[int, str]]) -> None:
        """Persist the fetched CDX pages so interrupted runs can resume."""

        self.layout.backup_path.mkdir(parents=True, exist_ok=True)
        payload = [[timestamp, url] for timestamp, url in snapshots]
        self.layout.cdx_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def load_downloaded_ids(self, mapper: LocalPathMapper) -> set[str]:
        """Load only DB entries that still exist on disk.

        This mirrors the Ruby behavior of distrusting stale resume entries after
        manual file deletion or an interrupted run that left the DB ahead of the
        actual filesystem.
        """

        if not self.layout.db_path.exists():
            return set()
        downloaded: set[str] = set()
        try:
            for line in self.layout.db_path.read_text(encoding="utf-8").splitlines():
                file_id = line.strip()
                if not file_id:
                    continue
                if mapper.local_path_for(file_id).exists():
                    downloaded.add(file_id)
        except OSError as exc:
            self.logger.warning("Failed to read download state %s: %s", self.layout.db_path, exc)
        return downloaded

    def append_downloaded_id(self, file_id: str) -> None:
        """Append a successful logical file ID to the resume database."""

        self.layout.backup_path.mkdir(parents=True, exist_ok=True)
        with self._db_lock:
            with self.layout.db_path.open("a", encoding="utf-8") as handle:
                handle.write(f"{file_id}\n")

    def cleanup(self, *, keep_state: bool, reset_requested: bool, had_failures: bool) -> None:
        """Remove state files unless the run should remain resumable."""

        if had_failures and not reset_requested:
            self.logger.info("Keeping state files because the download finished with errors.")
            return
        if reset_requested or not keep_state:
            self.layout.cdx_path.unlink(missing_ok=True)
            self.layout.db_path.unlink(missing_ok=True)
