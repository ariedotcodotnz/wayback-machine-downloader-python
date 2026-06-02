from __future__ import annotations

import logging
import queue
import re
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

from .archive import ArchiveClient
from .config import DownloadConfig
from .filters import URLFilter
from .models import DownloadSummary, FailedDownload, Snapshot
from .paths import LocalPathMapper, OutputLayout
from .requisites import PageRequisitesExtractor
from .snapshots import SnapshotPlanner
from .state import DownloadState
from .subdomains import SubdomainDiscovery
from .text import decode_best_effort
from .transport import ArchiveTransport
from .url_rewrite import LocalLinkRewriter


@dataclass(slots=True)
class _Progress:
    total: int = 0
    completed: int = 0


class WaybackDownloader:
    HTML_SUFFIXES = {".html", ".htm", ".php", ".asp", ".aspx", ".jsp"}
    REQUISITE_SKIP_SUFFIXES = {"", ".html", ".htm", ".php", ".asp", ".aspx", ".jsp"}
    _WAYBACK_EMBED_RE = re.compile(r"^/web/([0-9]{4,})[^/]*/(https?://.+)$")

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
        self.rewriter = LocalLinkRewriter()
        self._session_downloaded_ids: set[str] = set()
        self._session_lock = threading.Lock()
        self._print_lock = threading.Lock()
        self._progress = _Progress()
        self._failures: list[FailedDownload] = []
        # Built lazily from the site-wide CDX listing so page-requisite assets
        # can resolve their nearest archived timestamp without making one CDX
        # request per asset (the original behavior swamped the API).
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

        if not remaining and not discovery_jobs:
            self._cleanup()
            return DownloadSummary(
                root=self.layout.backup_path,
                discovered=len(planned_snapshots),
                queued=0,
                completed=0,
                skipped_existing=skipped_existing,
                failures=list(self._failures),
            )

        self._progress = _Progress(total=len(initial_jobs), completed=0)
        job_queue: queue.Queue[Snapshot | None] = queue.Queue()
        worker_count = max(1, min(self.config.concurrency, 10))
        workers = [
            threading.Thread(target=self._worker, args=(job_queue,), name=f"wayback-worker-{index}", daemon=True)
            for index in range(worker_count)
        ]
        for worker in workers:
            worker.start()

        for snapshot in remaining:
            job_queue.put(snapshot)
        for snapshot in discovery_jobs:
            job_queue.put(snapshot)

        job_queue.join()
        for _ in workers:
            job_queue.put(None)
        for worker in workers:
            worker.join()

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

    def _worker(self, job_queue: queue.Queue[Snapshot | None]) -> None:
        """Process downloads until a sentinel tells the worker to exit."""

        while True:
            snapshot = job_queue.get()
            if snapshot is None:
                job_queue.task_done()
                return
            network_used = False
            try:
                network_used = self._process_snapshot(snapshot, job_queue)
            finally:
                job_queue.task_done()
                if network_used:
                    time.sleep(self.config.rate_limit)

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
            if result.disposition.value == "not_found":
                self._mark_completed("NOT FOUND", snapshot.original_url)
                return True

            local_path.write_bytes(result.body or b"")

            # When rewriting and page-requisites are both on, ask the rewriter
            # to report every absolute URL it touched. The page-requisites
            # extractor only scans HTML href/src; the rewriter also catches
            # URLs in JS string literals and JSON-escaped script blocks. Those
            # additional URLs feed the same download queue so the rewritten
            # local paths actually have files behind them.
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
        """Scan a saved HTML page and queue its linked asset captures.

        The Ruby implementation used a per-session set so the same asset would
        not be queued forever when pages link to each other repeatedly.
        """

        content = decode_best_effort(file_path.read_bytes())
        parent_url = parent_snapshot.original_url
        if "://" not in parent_url:
            parent_url = f"http://{parent_url}"

        assets = PageRequisitesExtractor.extract(content)
        current_project_host = self._target_host()
        if current_project_host is None:
            return

        for asset_reference in assets:
            try:
                resolved = urljoin(parent_url, asset_reference)
                parsed = urlsplit(resolved)
                hint_timestamp = parent_snapshot.timestamp

                wayback_match = self._WAYBACK_EMBED_RE.match(parsed.path)
                if wayback_match:
                    # Reference is already a /web/{ts}/url embed; prefer its
                    # timestamp as the hint so we hit the same capture the
                    # original page linked to.
                    hint_timestamp = int(wayback_match.group(1))
                    parsed = urlsplit(wayback_match.group(2))

                extension = Path(parsed.path).suffix.lower()
                if extension in self.REQUISITE_SKIP_SUFFIXES:
                    continue

                asset_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
                self._queue_asset_for_url(asset_url, hint_timestamp, job_queue)
            except Exception:
                continue

    def _queue_asset_for_url(
        self,
        asset_url: str,
        hint_timestamp: int,
        job_queue: queue.Queue[Snapshot | None],
    ) -> None:
        """Convert an absolute URL into a Snapshot and queue it for download.

        Shared between the page-requisites HTML scan and the rewriter's URL
        collection: both discovery paths funnel through the same dedup,
        file_id, and timestamp-resolution logic.
        """

        try:
            parsed = urlsplit(asset_url)
            if not parsed.scheme or not parsed.hostname:
                return
            normalized_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
            current_project_host = self._target_host()
            if parsed.hostname == current_project_host:
                asset_file_id = parsed.path.lstrip("/")
                if parsed.query:
                    asset_file_id = f"{asset_file_id}?{parsed.query}"
            else:
                asset_file_id = normalized_url
            asset_timestamp = self._resolve_asset_timestamp(normalized_url, hint_timestamp)
            snapshot = Snapshot(
                original_url=normalized_url,
                timestamp=asset_timestamp,
                file_id=self.mapper.sanitize_file_id(asset_file_id, normalized_url),
            )
            self._queue_additional_snapshot(snapshot, job_queue)
        except Exception:
            return

    def _queue_additional_snapshot(self, snapshot: Snapshot, job_queue: queue.Queue[Snapshot | None]) -> None:
        with self._session_lock:
            if snapshot.file_id in self._session_downloaded_ids:
                return
            self._session_downloaded_ids.add(snapshot.file_id)
            self._progress.total += 1
        job_queue.put(snapshot)

    def _resolve_asset_timestamp(self, asset_url: str, parent_timestamp: int) -> int:
        """Pick the newest asset snapshot at or before the parent page time.

        The original implementation issued one CDX search per asset, which on
        large WordPress-style sites would queue hundreds of slow API calls (one
        per linked CSS/JS/image). Same-site assets are already enumerated in
        the site-wide CDX listing we cached at startup, so this now does an
        in-memory lookup. For assets that are not in the index (typically
        cross-origin CDN URLs) we fall back to ``parent_timestamp`` and let
        Wayback's ``id_`` endpoint redirect to its closest capture — the
        download path already follows those 302s.
        """

        index = self._asset_snapshot_index
        if index is not None:
            timestamps = index.get(self._asset_index_key(asset_url))
            if timestamps:
                eligible = [timestamp for timestamp in timestamps if timestamp <= parent_timestamp]
                return max(eligible) if eligible else max(timestamps)
        return parent_timestamp

    def _planned_snapshots(self) -> list[Snapshot]:
        raw_snapshots = self._raw_snapshots()
        if self._asset_snapshot_index is None:
            self._asset_snapshot_index = self._build_asset_index(raw_snapshots)
        return self.planner.build(
            raw_snapshots,
            all_timestamps=self.config.all_timestamps,
            snapshot_at=self.config.snapshot_at,
        )

    @staticmethod
    def _build_asset_index(raw_snapshots: list[tuple[int, str]]) -> dict[str, list[int]]:
        """Group the cached CDX rows by URL so asset lookups are O(1)."""

        index: dict[str, list[int]] = {}
        for timestamp, url in raw_snapshots:
            index.setdefault(WaybackDownloader._asset_index_key(url), []).append(int(timestamp))
        return index

    @staticmethod
    def _asset_index_key(url: str) -> str:
        """Build a scheme-insensitive lookup key (host + path + query)."""

        parsed = urlsplit(url if "://" in url else f"http://{url}")
        host = (parsed.hostname or "").lower()
        path = parsed.path or "/"
        return f"{host}{path}?{parsed.query}" if parsed.query else f"{host}{path}"

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
        files = [path for path in self.layout.backup_path.rglob("*") if path.suffix.lower() in {".html", ".htm", ".css", ".js"}]
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
                queue_by_depth = [f"{subdomain}.{base_domain}" for subdomain in discovered if f"{subdomain}.{base_domain}" not in processed_domains]
            depth += 1

        if self.config.rewrite_to_local:
            self.rewriter.rewrite_subdomain_links(self.layout.backup_path, processed_domains - {base_domain})

    def _mark_completed(self, status: str, url: str, *, error: str | None = None) -> None:
        with self._print_lock:
            self._progress.completed += 1
            message = f"[{status}] {url} ({self._progress.completed}/{self._progress.total})"
            if error:
                message = f"{message} {error}"
            self.logger.info(message)

    def _existing_html_requisite_jobs(self, planned_snapshots: list[Snapshot], downloaded_ids: set[str]) -> list[Snapshot]:
        """Seed page-requisite discovery from already-downloaded HTML files.

        This fixes the resume case where the user turns on ``--page-requisites``
        after a previous site download. The original port only queued missing
        files, so existing HTML pages never got a chance to discover their
        linked assets on a later run.
        """

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
        candidate = self.config.target if "://" in self.config.target else f"https://{self.config.target}"
        return urlsplit(candidate).hostname

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
