from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from os import name as os_name
from pathlib import Path
from typing import Iterable

from .paths import sanitize_reference_path
from .text import decode_with_candidates


class LocalLinkRewriter:
    SERVER_SIDE_EXTS = {".php", ".asp", ".aspx", ".jsp", ".cgi", ".pl", ".py"}
    REWRITE_SUFFIXES = {".html", ".htm", ".css", ".js", ".php", ".asp", ".aspx", ".jsp"}

    def rewrite_tree(self, root: Path, concurrency: int) -> int:
        """Rewrite every supported file under ``root`` for local browsing."""

        files = [path for path in root.rglob("*") if path.suffix.lower() in self.REWRITE_SUFFIXES]
        if not files:
            return 0
        if concurrency <= 1:
            return sum(1 for path in files if self.rewrite_file(path, root))
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            return sum(executor.map(lambda path: 1 if self.rewrite_file(path, root) else 0, files))

    def rewrite_file(self, file_path: Path, site_root: Path) -> bool:
        """Rewrite archived/absolute URLs in a downloaded file to local paths."""

        raw = file_path.read_bytes()
        preferred_encoding = self._detect_meta_charset(raw) if file_path.suffix.lower() in {".html", ".htm", ".php", ".asp"} else None
        content, encoding = decode_with_candidates(raw, preferred_encoding)
        original = content

        content = self.rewrite_html_attribute_urls(content)
        content = self.rewrite_css_urls(content)
        content = self.rewrite_js_urls(content)

        root_prefix = self.site_root_relative_prefix(file_path, site_root)
        content = re.sub(
            r"""(\s(?:href|src|action|data-src|data-url)=["'])/([^"'/][^"']*)(["'])""",
            lambda match: f"{match.group(1)}{root_prefix}{match.group(2)}{match.group(3)}",
            content,
            flags=re.IGNORECASE,
        )
        content = re.sub(
            r"""url\(\s*["']?/([^"')/][^"')]*?)["']?\s*\)""",
            lambda match: f'url("{root_prefix}{match.group(1)}")',
            content,
            flags=re.IGNORECASE,
        )

        if content == original:
            return False

        file_path.write_bytes(content.encode(encoding, errors="replace"))
        return True

    def rewrite_subdomain_links(self, root: Path, subdomains: Iterable[str]) -> int:
        """Point cross-subdomain URLs at the local ``subdomains/`` mirror."""

        files = [path for path in root.rglob("*") if path.suffix.lower() in {".html", ".htm", ".css", ".js"}]
        rewritten = 0
        unique_subdomains = sorted(set(subdomains))
        for file_path in files:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
            original = content
            root_prefix = self.site_root_relative_prefix(file_path, root)
            for subdomain_host in unique_subdomains:
                destination_prefix = f"{root_prefix}subdomains/{subdomain_host}"
                content = re.sub(
                    rf"""(\s(?:href|src|action|data-src|data-url)=["'])https?://{re.escape(subdomain_host)}([^"']*)(["'])""",
                    lambda match: f"{match.group(1)}{destination_prefix}{self._normalize_subdomain_path(match.group(2))}{match.group(3)}",
                    content,
                    flags=re.IGNORECASE,
                )
                content = re.sub(
                    rf"""url\(\s*["']?https?://{re.escape(subdomain_host)}([^"')]*?)["']?\s*\)""",
                    lambda match: f'url("{destination_prefix}{self._normalize_subdomain_path(match.group(1))}")',
                    content,
                    flags=re.IGNORECASE,
                )
                content = re.sub(
                    rf"""(["'])https?://{re.escape(subdomain_host)}([^"']*)(["'])""",
                    lambda match: f"{match.group(1)}{destination_prefix}{self._normalize_subdomain_path(match.group(2))}{match.group(3)}",
                    content,
                    flags=re.IGNORECASE,
                )
            if content != original:
                file_path.write_text(content, encoding="utf-8")
                rewritten += 1
        return rewritten

    def rewrite_html_attribute_urls(self, content: str) -> str:
        # Rewrite Wayback-hosted URLs first so we preserve the original path.
        content = re.sub(
            r"""(\s(?:href|src|action|data-src|data-url)=["'])https?://web\.archive\.org/web/\d+(?:id_)?/https?://[^/]+([^"']*)(["'])""",
            lambda match: f"{match.group(1)}{self.normalize_path_for_local(match.group(2))}{match.group(3)}",
            content,
            flags=re.IGNORECASE,
        )
        # Then rewrite regular absolute same/off-site links into local paths.
        return re.sub(
            r"""(\s(?:href|src|action|data-src|data-url)=["'])https?://[^/]+([^"']*)(["'])""",
            lambda match: f"{match.group(1)}{self.normalize_path_for_local(match.group(2))}{match.group(3)}",
            content,
            flags=re.IGNORECASE,
        )

    def rewrite_css_urls(self, content: str) -> str:
        # CSS url(...) references appear in archived HTML and in standalone CSS.
        content = re.sub(
            r"""url\(\s*["']?https?://web\.archive\.org/web/\d+(?:id_)?/https?://[^/]+([^"')]*?)["']?\s*\)""",
            lambda match: f'url("{self.normalize_path_for_local(match.group(1))}")',
            content,
            flags=re.IGNORECASE,
        )
        return re.sub(
            r"""url\(\s*["']?https?://[^/]+([^"')]*?)["']?\s*\)""",
            lambda match: f'url("{self.normalize_path_for_local(match.group(1))}")',
            content,
            flags=re.IGNORECASE,
        )

    def rewrite_js_urls(self, content: str) -> str:
        # JavaScript string literals often embed full absolute URLs.
        content = re.sub(
            r"""(["'])https?://web\.archive\.org/web/\d+(?:id_)?/https?://[^/]+([^"']*)(["'])""",
            lambda match: f"{match.group(1)}{self.normalize_path_for_local(match.group(2))}{match.group(3)}",
            content,
            flags=re.IGNORECASE,
        )
        return re.sub(
            r"""(["'])https?://[^/]+([^"']*)(["'])""",
            lambda match: f"{match.group(1)}{self.normalize_path_for_local(match.group(2))}{match.group(3)}",
            content,
            flags=re.IGNORECASE,
        )

    def normalize_path_for_local(self, path: str) -> str:
        """Convert an archived absolute path into the downloaded local path.

        Query strings are not discarded. They are folded into the filename using
        the same ``__q<digest>`` convention that the downloader uses on disk, so
        rewritten references still point at the saved file.
        """

        if not path or path == "/":
            return "./index.html"

        normalized_tail = sanitize_reference_path(path, filesystem_safe=os_name == "nt")
        if not normalized_tail:
            return "./index.html"

        relative_path = f"./{normalized_tail}"
        extension = Path(normalized_tail).suffix.lower()
        if extension in self.SERVER_SIDE_EXTS:
            return relative_path

        basename = Path(normalized_tail).name
        if path.endswith("/") or "." not in basename:
            return f"{relative_path.rstrip('/')}/index.html"
        return relative_path

    @staticmethod
    def site_root_relative_prefix(file_path: Path, site_root: Path) -> str:
        try:
            relative_dir = file_path.resolve().parent.relative_to(site_root.resolve())
        except ValueError:
            return "./"
        depth = len(relative_dir.parts)
        return "./" if depth == 0 else "../" * depth

    @staticmethod
    def _detect_meta_charset(raw: bytes) -> str | None:
        match = re.search(br"""<meta\s+charset=["']?([^"'>\s]+)""", raw, re.IGNORECASE)
        if not match:
            return None
        return match.group(1).decode("ascii", errors="ignore") or None

    @staticmethod
    def _normalize_subdomain_path(path: str) -> str:
        return "/index.html" if path in {"", "/"} else path
