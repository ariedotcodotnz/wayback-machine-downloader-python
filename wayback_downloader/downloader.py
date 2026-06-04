from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import replace
from pathlib import Path

from .archive import ArchiveClient
from .config import DownloadConfig
from .download import (
    AssetSnapshotIndex,
    AssetSnapshotPlanner,
    DownloadProgress,
    SnapshotDownloadQueue,
)
from .filters import URLFilter
from .models import DownloadSummary, FailedDownload, FetchDisposition, Snapshot
from .paths import LocalPathMapper, OutputLayout
from .snapshots import SnapshotPlanner
from .state import DownloadState
from .subdomains import SubdomainDiscovery
from .transport import ArchiveTransport
from .url_rewrite import LocalLinkRewriter


class WaybackDownloader:
    """Coordinate site mirroring through focused downloader services."""

    HTML_SUFFIXES = AssetSnapshotPlanner.HTML_SUFFIXES
    REQUISITE_SKIP_SUFFIXES = AssetSnapshotPlanner.REQUISITE_SKIP_SUFFIXES

    def __init__(
        self,
        config: DownloadConfig,
        *,
        transport: ArchiveTransport | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.logger = logger or self._create_logger()
        self.layout = OutputLayout(config)
        self.mapper = LocalPathMapper(self.layout.backup_path)
        self.filters = URLFilter(config.only_filter, config.exclude_filter)
        self.state = DownloadState(self.layout, self.logger)
        self.archive = ArchiveClient(config, transport=transport, logger=self.logger)
        self.planner = SnapshotPlanner(self.filters, self.mapper)
        self.asset_planner = AssetSnapshotPlanner(config, self.mapper)
        self.rewriter = LocalLinkRewriter()

        self._session_downloaded_ids: set[str] = set()
        self._session_lock = threading.Lock()
        self._progress = DownloadProgress()
        self._failures: list[FailedDownload] = []
        self._asset_snapshot_index: dict[str, list[int]] | None = None

        if self.config.reset:
            self.state.reset()

    def list_files(self) -> list[dict[str, int | str]]:
        """Return the planned capture list in the JSON-friendly CLI shape."""

        return [
            {"file_url": snapshot.original_url, "timestamp": snapshot.timestamp, "file_id": snapshot.file_id}
            for snapshot in self._planned_snapshots()
        ]

    def rewrite_local_files(self) -> int:
        """Rewrite an already-downloaded tree without fetching anything new."""

        self.layout.backup_path.mkdir(parents=True, exist_ok=True)
        return self.rewriter.rewrite_tree(self.layout.backup_path, self.config.concurrency)

    def download(self) -> DownloadSummary:
        """Download the planned captures and any queued dependent assets."""

        start_time = time.perf_counter()
        self.layout.backup_path.mkdir(parents=True, exist_ok=True)

        planned_snapshots = self._planned_snapshots()
        if not planned_snapshots:
            self._cleanup()
            return DownloadSummary(
                root=self.layout.backup_path,
                discovered=0,
                queued=0,
                completed=0,
                skipped_existing=0,
                failures=list(self._failures),
            )

        downloaded_ids = self.state.load_downloaded_ids(self.mapper)
        self._session_downloaded_ids = set(downloaded_ids)
        remaining = [snapshot for snapshot in planned_snapshots if snapshot.file_id not in downloaded_ids]
        skipped_existing = len(planned_snapshots) - len(remaining)
        discovery_jobs = self._existing_html_requisite_jobs(planned_snapshots, downloaded_ids)
        initial_jobs = [*remaining, *discovery_jobs]

        if not initial_jobs:
            self._cleanup()
            return DownloadSummary(
                root=self.layout.backup_path,
                discovered=len(planned_snapshots),
                queued=0,
                completed=0,
                skipped_existing=skipped_existing,
                failures=list(self._failures),
            )

        self._progress.reset(len(initial_jobs))
        SnapshotDownloadQueue(
            worker_count=max(1, min(self.config.concurrency, 10)),
            rate_limit=self.config.rate_limit,
            processor=self._process_snapshot,
        ).run(initial_jobs)

        if self.config.recursive_subdomains:
            self._process_subdomains()

        self._cleanup()
        self.logger.info("Download finished in %.2fs", time.perf_counter() - start_time)
        return DownloadSummary(
            root=self.layout.backup_path,
            discovered=len(planned_snapshots),
            queued=self._progress.total,
            completed=self._progress.completed,
            skipped_existing=skipped_existing,
            failures=list(self._failures),
        )

    def _process_snapshot(self, snapshot: Snapshot, job_queue: queue.Queue[Snapshot | None]) -> bool:
        """Download one capture or reuse an already-downloaded local file.

        Returns ``True`` only when a real HTTP fetch was attempted, which lets
        the worker skip rate-limit sleeps for the fast ``[EXISTS]`` path.
        """

        local_path = self.mapper.local_path_for(snapshot.file_id, snapshot.original_url)
        if local_path.exists():
            self.state.append_downloaded_id(snapshot.file_id)
            self._mark_completed("EXISTS", snapshot.original_url)
            if self.config.page_requisites and local_path.suffix.lower() in self.HTML_SUFFIXES:
                self._process_page_requisites(local_path, snapshot, job_queue)
            return False

        try:
            self.mapper.ensure_directory(local_path.parent)
            result = self.archive.download_capture(snapshot.original_url, snapshot.timestamp)
            if result.disposition is FetchDisposition.NOT_FOUND:
                self._mark_completed("NOT FOUND", snapshot.original_url)
                return True

            local_path.write_bytes(result.body or b"")

            collected_urls: list[str] | None = None
            if self.config.rewrite_to_local and local_path.suffix.lower() in LocalLinkRewriter.REWRITE_SUFFIXES:
                if self.config.page_requisites:
                    collected_urls = []
                self.rewriter.rewrite_file(local_path, self.layout.backup_path, collected_urls=collected_urls)

            self.state.append_downloaded_id(snapshot.file_id)
            self._mark_completed("SAVED", snapshot.original_url)

            if self.config.page_requisites and local_path.suffix.lower() in self.HTML_SUFFIXES:
                self._process_page_requisites(local_path, snapshot, job_queue)

            if collected_urls:
                for collected_url in collected_urls:
                    self._queue_asset_for_url(collected_url, snapshot.timestamp, job_queue)
            return True
        except Exception as exc:
            if local_path.exists() and local_path.stat().st_size == 0:
                local_path.unlink(missing_ok=True)
            self._failures.append(FailedDownload(snapshot.original_url, str(exc)))
            self._mark_completed("FAILED", snapshot.original_url, error=str(exc))
            return True

    def _process_page_requisites(
        self,
        file_path: Path,
        parent_snapshot: Snapshot,
        job_queue: queue.Queue[Snapshot | None],
    ) -> None:
        """Scan a saved HTML page and queue its linked asset captures."""

        self._sync_asset_planner_index()
        for snapshot in self.asset_planner.discover_from_html(file_path, parent_snapshot):
            self._queue_additional_snapshot(snapshot, job_queue)

    def _queue_asset_for_url(
        self,
        asset_url: str,
        hint_timestamp: int,
        job_queue: queue.Queue[Snapshot | None],
    ) -> None:
        """Convert an absolute URL into a Snapshot and queue it for download."""

        self._sync_asset_planner_index()
        try:
            snapshot = self.asset_planner.snapshot_for_url(asset_url, hint_timestamp)
            if snapshot is None:
                return
            self._queue_additional_snapshot(snapshot, job_queue)
        except Exception:
            return

    def _queue_additional_snapshot(self, snapshot: Snapshot, job_queue: queue.Queue[Snapshot | None]) -> None:
        with self._session_lock:
            if snapshot.file_id in self._session_downloaded_ids:
                return
            self._session_downloaded_ids.add(snapshot.file_id)
            self._progress.add_total()
        job_queue.put(snapshot)

    def _resolve_asset_timestamp(self, asset_url: str, parent_timestamp: int) -> int:
        """Pick the newest asset snapshot at or before the parent page time."""

        if self._asset_snapshot_index is None:
            return parent_timestamp
        return AssetSnapshotIndex(self._asset_snapshot_index).resolve(asset_url, parent_timestamp)

    def _planned_snapshots(self) -> list[Snapshot]:
        raw_snapshots = self._raw_snapshots()
        if self._asset_snapshot_index is None:
            self._set_asset_snapshot_index(self._build_asset_index(raw_snapshots))
        return self.planner.build(
            raw_snapshots,
            all_timestamps=self.config.all_timestamps,
            snapshot_at=self.config.snapshot_at,
        )

    @staticmethod
    def _build_asset_index(raw_snapshots: list[tuple[int, str]]) -> dict[str, list[int]]:
        """Group the cached CDX rows by URL so asset lookups are O(1)."""

        return AssetSnapshotIndex.from_raw_snapshots(raw_snapshots).timestamps_by_url

    @staticmethod
    def _asset_index_key(url: str) -> str:
        """Build a scheme-insensitive lookup key from host, path, and query."""

        return AssetSnapshotIndex.key(url)

    def _set_asset_snapshot_index(self, index: dict[str, list[int]] | None) -> None:
        self._asset_snapshot_index = index
        self._sync_asset_planner_index()

    def _sync_asset_planner_index(self) -> None:
        if self._asset_snapshot_index is None:
            self.asset_planner.snapshot_index = None
            return
        self.asset_planner.snapshot_index = AssetSnapshotIndex(self._asset_snapshot_index)

    def _raw_snapshots(self) -> list[tuple[int, str]]:
        cached = None if self.config.reset else self.state.load_snapshot_cache()
        if cached is not None:
            return cached
        snapshots = self.archive.fetch_all_snapshots(self.config.target)
        self.state.save_snapshot_cache(snapshots)
        return snapshots

    def _process_subdomains(self) -> None:
        """Recursively mirror discovered subdomains into ``subdomains/``."""

        base_domain = SubdomainDiscovery.extract_base_domain(self.config.target)
        if not base_domain:
            return

        processed_domains = {base_domain}
        files = [
            path
            for path in self.layout.backup_path.rglob("*")
            if path.suffix.lower() in {".html", ".htm", ".css", ".js"}
        ]
        found = SubdomainDiscovery.scan_files(files, base_domain)
        queue_by_depth = [f"{subdomain}.{base_domain}" for subdomain in found]

        depth = 0
        while depth < self.config.subdomain_depth and queue_by_depth:
            current_batch = queue_by_depth
            queue_by_depth = []
            for host in current_batch:
                if host in processed_domains:
                    continue
                processed_domains.add(host)
                sub_config = replace(
                    self.config,
                    target=f"https://{host}/",
                    directory=self.layout.backup_path / "subdomains" / host,
                    maximum_pages=max(self.config.maximum_pages // 2, 10),
                    recursive_subdomains=False,
                    list_only=False,
                    local_only=False,
                )
                WaybackDownloader(sub_config, logger=self.logger).download()

            if depth + 1 < self.config.subdomain_depth:
                new_files = [
                    path
                    for path in (self.layout.backup_path / "subdomains").rglob("*")
                    if path.suffix.lower() in {".html", ".htm", ".css", ".js"}
                ]
                discovered = SubdomainDiscovery.scan_files(new_files, base_domain)
                queue_by_depth = [
                    f"{subdomain}.{base_domain}"
                    for subdomain in discovered
                    if f"{subdomain}.{base_domain}" not in processed_domains
                ]
            depth += 1

        if self.config.rewrite_to_local:
            self.rewriter.rewrite_subdomain_links(self.layout.backup_path, processed_domains - {base_domain})

    def _mark_completed(self, status: str, url: str, *, error: str | None = None) -> None:
        self._progress.mark_completed(status, url, self.logger, error=error)

    def _existing_html_requisite_jobs(self, planned_snapshots: list[Snapshot], downloaded_ids: set[str]) -> list[Snapshot]:
        """Seed page-requisite discovery from already-downloaded HTML files."""

        if not self.config.page_requisites:
            return []

        jobs: list[Snapshot] = []
        for snapshot in planned_snapshots:
            if snapshot.file_id not in downloaded_ids:
                self._session_downloaded_ids.add(snapshot.file_id)
                continue

            local_path = self.mapper.local_path_for(snapshot.file_id, snapshot.original_url)
            if local_path.suffix.lower() in self.HTML_SUFFIXES and local_path.exists():
                jobs.append(snapshot)
        return jobs

    def _target_host(self) -> str | None:
        return self.asset_planner.target_host()

    def _cleanup(self) -> None:
        try:
            self.state.cleanup(
                keep_state=self.config.keep_state,
                reset_requested=self.config.reset,
                had_failures=bool(self._failures),
            )
        finally:
            self.archive.close()

    @staticmethod
    def _create_logger() -> logging.Logger:
        logger = logging.getLogger("wayback_downloader")
        if logger.handlers:
            return logger
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        return logger
