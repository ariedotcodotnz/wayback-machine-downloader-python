from __future__ import annotations

import json
import logging
import re
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode, urljoin

from .config import DownloadConfig
from .models import FetchDisposition, FetchResult
from .transport import ArchiveTransport, HTTPArchiveTransport


class ArchiveClient:
    VERSION = "0.1.0"
    RETRY_DELAY = 2.0
    REDIRECT_LIMIT = 5
    _WAYBACK_URL_RE = re.compile(r"^https?://web\.archive\.org/web/")
    _WAYBACK_EXTRACT_RE = re.compile(r"^https?://web\.archive\.org/web/\d{1,14}(?:[a-z_]*)/(https?://.+)$")

    def __init__(
        self,
        config: DownloadConfig,
        transport: ArchiveTransport | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.transport = transport or HTTPArchiveTransport(config.timeout)
        self.logger = logger or logging.getLogger(__name__)

    def close(self) -> None:
        self.transport.close()

    def parameters_for_api(self, page_index: int | None, *, to_override: int | None = None) -> list[tuple[str, str]]:
        """Build the CDX query parameters used by the original Ruby tool."""

        parameters: list[tuple[str, str]] = [("fl", "timestamp,original"), ("gzip", "true")]
        if not self.config.keep_duplicates and not self.config.all_timestamps:
            parameters.append(("collapse", "digest"))
        if not self.config.include_all_responses:
            parameters.append(("filter", "statuscode:2..|30[12378]"))
        if self.config.from_timestamp:
            parameters.append(("from", str(self.config.from_timestamp)))
        effective_to = to_override if to_override is not None else self.config.to_timestamp
        if effective_to:
            parameters.append(("to", str(effective_to)))
        if page_index is not None:
            parameters.append(("page", str(page_index)))
        return parameters

    def fetch_snapshot_page(self, target_url: str, page_index: int, *, to_override: int | None = None) -> list[tuple[int, str]]:
        """Fetch one CDX page, retrying transient API and decode failures."""

        normalized_target = self.normalize_query_url(target_url)
        query = urlencode([("output", "json"), ("url", normalized_target), *self.parameters_for_api(page_index, to_override=to_override)])
        request_url = f"https://web.archive.org/cdx/search/cdx?{query}"
        retries = 0

        while True:
            try:
                response = self.transport.get(
                    request_url,
                    headers={
                        "User-Agent": f"wayback-downloader-python/{self.VERSION}",
                        "Connection": "keep-alive",
                        "Accept-Encoding": "gzip",
                    },
                )
                if response.status == 200:
                    body = self._decode_body(response.body, response.headers, source=request_url)
                    payload = body.decode("utf-8").strip()
                    if not payload:
                        return []
                    parsed = json.loads(payload)
                    if parsed and parsed[0] == ["timestamp", "original"]:
                        parsed = parsed[1:]
                    return [(int(timestamp), str(url)) for timestamp, url in parsed]
                if response.status in {429, 500, 502, 503, 504}:
                    raise RuntimeError(f"Wayback CDX API error {response.status}: {response.reason}")
                self.logger.warning("Unexpected CDX response %s for %s", response.status, normalized_target)
                return []
            except Exception as exc:
                if retries >= self.config.max_retries:
                    self.logger.warning("Giving up on the CDX API for %s: %s", normalized_target, exc)
                    return []
                retries += 1
                self.logger.warning(
                    "Retrying CDX page %s for %s (%s/%s): %s",
                    page_index,
                    normalized_target,
                    retries,
                    self.config.max_retries,
                    exc,
                )
                time.sleep(self.RETRY_DELAY * retries)

    def fetch_all_snapshots(self, target_url: str) -> list[tuple[int, str]]:
        to_override = self.config.snapshot_at
        snapshots = self.fetch_snapshot_page(target_url, 0, to_override=to_override)
        if self.config.exact_url or not snapshots:
            return snapshots

        page_index = 1
        batch_size = min(self.config.concurrency, 5)
        executor_workers = max(1, min(self.config.concurrency, 5))

        with ThreadPoolExecutor(max_workers=executor_workers) as executor:
            while page_index < self.config.maximum_pages:
                end_index = min(page_index + batch_size, self.config.maximum_pages)
                page_numbers = list(range(page_index, end_index))
                results = list(
                    executor.map(
                        lambda page: (page, self.fetch_snapshot_page(target_url, page, to_override=to_override)),
                        page_numbers,
                    )
                )
                stop_after_batch = False
                for _, page_snapshots in sorted(results, key=lambda item: item[0]):
                    if not page_snapshots:
                        stop_after_batch = True
                        break
                    snapshots.extend(page_snapshots)
                if stop_after_batch:
                    break
                page_index = end_index
                time.sleep(self.config.rate_limit)

        return snapshots

    def build_wayback_url(self, source_url: str, timestamp: int) -> str:
        if self._WAYBACK_URL_RE.match(source_url):
            return source_url
        if source_url.startswith("/web/"):
            return f"https://web.archive.org{source_url}"
        suffix = "" if self.config.rewritten else "id_"
        return f"https://web.archive.org/web/{timestamp}{suffix}/{source_url}"

    def extract_original_url(self, url: str) -> str | None:
        match = self._WAYBACK_EXTRACT_RE.match(url)
        return match.group(1) if match else None

    def resolve_redirect_source(self, current_source_url: str, location: str) -> str | None:
        if not location:
            return None
        if self._WAYBACK_URL_RE.match(location):
            return location
        if location.startswith("/web/"):
            return f"https://web.archive.org{location}"
        base_url = self.extract_original_url(current_source_url) or current_source_url
        return urljoin(base_url, location)

    def download_capture(self, source_url: str, timestamp: int) -> FetchResult:
        retries = 0
        redirect_count = 0
        current_source_url = source_url

        while True:
            try:
                request_url = self.build_wayback_url(current_source_url, timestamp)
                response = self.transport.get(
                    request_url,
                    headers={
                        "User-Agent": f"wayback-downloader-python/{self.VERSION}",
                        "Connection": "keep-alive",
                    "Accept-Encoding": "gzip, deflate",
                    },
                )

                if self.config.include_all_responses:
                    if 200 <= response.status < 600:
                        return FetchResult(
                            disposition=FetchDisposition.SAVED,
                            body=self._decode_body(response.body, response.headers, source=request_url),
                            source_url=current_source_url,
                            status_code=response.status,
                        )
                    raise RuntimeError(f"Unhandled HTTP response {response.status}: {response.reason}")

                if 200 <= response.status < 300:
                    return FetchResult(
                        disposition=FetchDisposition.SAVED,
                        body=self._decode_body(response.body, response.headers, source=request_url),
                        source_url=current_source_url,
                        status_code=response.status,
                    )
                if 300 <= response.status < 400:
                    if redirect_count >= self.REDIRECT_LIMIT:
                        raise RuntimeError(f"Too many redirects for {current_source_url}")
                    redirected_source = self.resolve_redirect_source(current_source_url, response.headers.get("location", ""))
                    if not redirected_source:
                        raise RuntimeError(f"Redirect without location for {current_source_url}")
                    current_source_url = redirected_source
                    redirect_count += 1
                    continue
                if response.status == 404:
                    return FetchResult(
                        disposition=FetchDisposition.NOT_FOUND,
                        body=None,
                        source_url=current_source_url,
                        status_code=response.status,
                    )
                if response.status == 429:
                    raise RuntimeError("Rate limited by the archive")
                raise RuntimeError(f"HTTP error {response.status}: {response.reason}")
            except Exception:
                if retries >= self.config.max_retries:
                    raise
                retries += 1
                time.sleep(self.RETRY_DELAY * retries)

    def normalize_query_url(self, target_url: str) -> str:
        if self.config.exact_url:
            return target_url
        normalized = target_url.strip()
        if "*" in normalized:
            return normalized
        stripped = re.sub(r"^https?://", "", normalized, flags=re.IGNORECASE)
        host_and_rest = re.split(r"[?#]", stripped, maxsplit=1)[0]
        if "/" not in host_and_rest:
            return f"{normalized}/*"
        return normalized

    def _decode_body(self, body: bytes, headers: dict[str, str] | object, *, source: str) -> bytes:
        if not body:
            return body
        encoding = ""
        if isinstance(headers, dict):
            encoding = headers.get("content-encoding", "").lower()
        if encoding == "gzip":
            try:
                return zlib.decompress(body, zlib.MAX_WBITS | 16)
            except zlib.error as exc:
                # Ruby fell back to the raw body here so a bad Content-Encoding
                # header would not lose the capture entirely.
                self.logger.warning("Failed to decode gzip body for %s: %s. Keeping raw bytes.", source, exc)
                return body
        if encoding == "deflate":
            try:
                return zlib.decompress(body)
            except zlib.error:
                try:
                    return zlib.decompress(body, -zlib.MAX_WBITS)
                except zlib.error as exc:
                    self.logger.warning("Failed to decode deflate body for %s: %s. Keeping raw bytes.", source, exc)
                    return body
        return body
