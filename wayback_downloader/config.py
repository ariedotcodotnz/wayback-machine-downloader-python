from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(slots=True)
class DownloadConfig:
    target: str
    directory: Path | None = None
    all_timestamps: bool = False
    from_timestamp: int | None = None
    to_timestamp: int | None = None
    exact_url: bool = False
    only_filter: str | None = None
    exclude_filter: str | None = None
    include_all_responses: bool = False
    keep_duplicates: bool = False
    maximum_pages: int = 100
    concurrency: int = 1
    list_only: bool = False
    rewritten: bool = False
    rewrite_to_local: bool = False
    local_only: bool = False
    reset: bool = False
    keep_state: bool = False
    max_retries: int = 3
    snapshot_at: int | None = None
    recursive_subdomains: bool = False
    subdomain_depth: int = 1
    page_requisites: bool = False
    cross_host: bool = False
    timeout: float = 30.0
    rate_limit: float = 0.25

    def __post_init__(self) -> None:
        self.target = self.target.strip()
        if self.directory is not None:
            self.directory = Path(self.directory).expanduser().resolve()
        if self.maximum_pages <= 0:
            raise ValueError("maximum_pages must be positive")
        if self.concurrency <= 0:
            raise ValueError("concurrency must be positive")
        if self.max_retries < 0:
            raise ValueError("max_retries must not be negative")
        if self.subdomain_depth <= 0:
            raise ValueError("subdomain_depth must be positive")
        if not self.target and not self.local_only:
            raise ValueError("target is required")

    @property
    def backup_name(self) -> str:
        url_to_process = self.target
        if url_to_process.endswith("/*"):
            url_to_process = url_to_process[:-2]
        parsed = urlsplit(url_to_process)
        raw = parsed.netloc or parsed.path
        if "://" not in url_to_process and "/" in raw:
            raw = raw.split("/", 1)[0]
        if raw.startswith("*."):
            raw = raw.replace("*.", "all-", 1)
        sanitized = "".join("_" if char in '/:*?"<>|' else char for char in raw).rstrip(" .")
        return sanitized or "site"

    @property
    def output_path(self) -> Path:
        if self.directory is not None:
            return self.directory
        return (Path.cwd() / "websites" / self.backup_name).resolve()
