from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .config import DownloadConfig
from .text import decode_best_effort, repeated_percent_decode


_CONTROL_CHARS = re.compile(r"[\x00-\x1F]")
_WINDOWS_INVALID = re.compile(r'[:*?"<>|\\]')
_WINDOWS_FS_INVALID = re.compile(r'[:*?"<>|&=\\/]')
_HTML_COMMENT_PREFIX = re.compile(r"<!--+")


def _apply_query_digest(path_part: str, query_part: str) -> str:
    """Mirror the Ruby strategy of folding queries into the local filename.

    The downloader stores query-bearing URLs as stable filenames by hashing the
    query string and inserting that digest before the extension when possible.
    """

    if not query_part:
        return path_part

    digest = hashlib.sha256(query_part.encode("utf-8")).hexdigest()[:12]
    if "." in path_part:
        prefix, dot, suffix = path_part.rpartition(".")
        return f"{prefix}__q{digest}{dot}{suffix}"
    return f"{path_part}__q{digest}"


def sanitize_reference_path(raw: str, *, filesystem_safe: bool = False) -> str:
    """Sanitize a URL tail so rewritten local links match downloaded files.

    This intentionally shares the same cleanup rules as the on-disk file ID
    logic: repeatedly percent-decode, repair bytes best-effort, strip comment
    fragments/control bytes, hash queries, and sanitize each path segment.
    """

    decoded_bytes = repeated_percent_decode(raw)
    text = decode_best_effort(decoded_bytes)
    text = _HTML_COMMENT_PREFIX.sub("", text)
    text = _CONTROL_CHARS.sub("", text)

    path_part, _, query_part = text.partition("?")
    normalized = re.sub(r"/+", "/", _apply_query_digest(path_part, query_part)).lstrip("/")
    segments: list[str] = []
    for segment in normalized.split("/"):
        # Skip empty segments (e.g. from a trailing slash) so they don't get
        # promoted to "_" by the all-invalid-chars fallback below; trailing
        # slashes are a directory indicator, not a real path component.
        if not segment:
            continue
        cleaned = _sanitize_identifier_segment(segment)
        if filesystem_safe:
            cleaned = _filesystem_safe_segment(cleaned)
        segments.append(cleaned)
    return "/".join(segment for segment in segments if segment is not None)


@dataclass(slots=True)
class OutputLayout:
    config: DownloadConfig

    @property
    def backup_name(self) -> str:
        return self.config.backup_name

    @property
    def backup_path(self) -> Path:
        return self.config.output_path

    @property
    def cdx_path(self) -> Path:
        return self.backup_path / ".cdx.json"

    @property
    def db_path(self) -> Path:
        return self.backup_path / ".downloaded.txt"


class LocalPathMapper:
    def __init__(self, root: Path) -> None:
        self.root = root

    def sanitize_file_id(self, raw: str, source_url: str) -> str:
        """Convert the archived URL tail into a stable logical file ID.

        The original Ruby code took care to percent-decode repeatedly, rescue
        broken encodings, and hash query strings so files like
        ``style.css?ver=123`` map to a deterministic local filename. We keep
        the same behavior here because it affects downloading, resume state, and
        later local-link rewriting.
        """

        if raw == "":
            return ""
        original = raw
        try:
            sanitized = sanitize_reference_path(raw)
            return sanitized or f"file__{hashlib.sha1(original.encode('utf-8', 'replace')).hexdigest()[:10]}"
        except Exception:
            return f"file__{hashlib.sha1(original.encode('utf-8', 'replace')).hexdigest()[:10]}"

    def local_path_for(self, file_id: str, source_url: str | None = None) -> Path:
        """Resolve the logical file ID to the actual filesystem path.

        Directory-like captures are stored as ``.../index.html`` so that the
        downloaded tree works the same way as a static website when opened
        locally.
        """

        if file_id == "":
            return self.root / "index.html"

        raw_segments = [segment for segment in file_id.split("/") if segment]
        filesystem_segments = [_filesystem_safe_segment(segment) for segment in raw_segments]
        looks_like_directory = file_id.endswith("/") or not Path(raw_segments[-1]).suffix
        if source_url and source_url.endswith("/"):
            looks_like_directory = True

        if looks_like_directory:
            return self.root.joinpath(*filesystem_segments) / "index.html"
        return self.root.joinpath(*filesystem_segments)

    def ensure_directory(self, directory: Path) -> None:
        """Create a directory, restructuring blocking files when necessary.

        A classic Wayback edge case is downloading ``/foo`` as a file and later
        discovering ``/foo/bar``. When that happens we move the blocking file to
        ``foo/index.html`` and retry so the tree can contain both captures.
        """

        try:
            directory.mkdir(parents=True, exist_ok=True)
            return
        except (FileExistsError, NotADirectoryError):
            pass

        blocking = self._find_blocking_file(directory)
        if blocking is None:
            directory.mkdir(parents=True, exist_ok=True)
            return

        temporary = blocking.with_name(f"{blocking.name}.temp")
        blocking.rename(temporary)
        blocking.mkdir(parents=True, exist_ok=True)
        temporary.rename(blocking / "index.html")
        self.ensure_directory(directory)

    def site_root_relative_prefix(self, file_path: Path) -> str:
        """Return ``./`` or enough ``../`` segments to reach the site root."""

        try:
            relative_dir = file_path.resolve().parent.relative_to(self.root.resolve())
        except ValueError:
            return "./"
        depth = len(relative_dir.parts)
        return "./" if depth == 0 else "../" * depth

    @staticmethod
    def _find_blocking_file(directory: Path) -> Path | None:
        parts = directory.resolve().parts
        if not parts:
            return None
        current = Path(parts[0])
        for part in parts[1:]:
            current = current / part
            if current.exists() and not current.is_dir():
                return current
        return None


def _sanitize_identifier_segment(segment: str) -> str:
    cleaned = _WINDOWS_INVALID.sub(lambda match: f"%{ord(match.group(0)):02X}", segment)
    cleaned = cleaned.replace("<", "").replace(">", "").rstrip(" .")
    return cleaned or "_"


def _filesystem_safe_segment(segment: str) -> str:
    if os.name != "nt":
        return segment
    cleaned = _WINDOWS_FS_INVALID.sub(lambda match: f"%{ord(match.group(0)):02X}", segment)
    return cleaned.rstrip(" .") or "_"
