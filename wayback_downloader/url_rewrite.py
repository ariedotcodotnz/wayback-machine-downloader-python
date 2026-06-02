from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from os import name as os_name
from pathlib import Path
from typing import Iterable

from .paths import sanitize_reference_path
from .text import decode_with_candidates


# Collection patterns mirror the substitution patterns below but additionally
# capture the host so the caller can reconstruct the original URL. They are
# defined alongside the substitution regexes so future edits to one side are
# obviously paired with the other.
_COLLECT_HTML_WAYBACK = re.compile(
    r"""\s(?:href|src|action|data-src|data-url)=["']https?://web\.archive\.org/web/\d+(?:id_)?/(?:https?:)?//([^/]+)([^"']*)["']""",
    re.IGNORECASE,
)
_COLLECT_HTML_STD = re.compile(
    r"""\s(?:href|src|action|data-src|data-url)=["'](?:https?:)?//([^/]+)([^"']*)["']""",
    re.IGNORECASE,
)
_COLLECT_CSS_WAYBACK = re.compile(
    r"""url\(\s*["']?https?://web\.archive\.org/web/\d+(?:id_)?/(?:https?:)?//([^/]+)([^"')]*?)["']?\s*\)""",
    re.IGNORECASE,
)
_COLLECT_CSS_STD = re.compile(
    r"""url\(\s*["']?(?:https?:)?//([^/]+)([^"')]*?)["']?\s*\)""",
    re.IGNORECASE,
)
_COLLECT_JS_WAYBACK = re.compile(
    r"""["']https?://web\.archive\.org/web/\d+(?:id_)?/(?:https?:)?//([^/]+)([^"']*)["']""",
    re.IGNORECASE,
)
_COLLECT_JS_STD = re.compile(
    r"""["'](?:https?:)?//([^/]+)([^"']*)["']""",
    re.IGNORECASE,
)
_COLLECT_JSON_WAYBACK = re.compile(
    r"""["']https?:\\/\\/web\.archive\.org\\/web\\/\d+(?:id_)?\\/(?:https?:)?\\/\\/([^\\"']+)((?:\\/[^"']*?)?)["']""",
    re.IGNORECASE,
)
_COLLECT_JSON_STD = re.compile(
    r"""["'](?:https?:)?\\/\\/([^\\"']+)((?:\\/[^"']*?)?)["']""",
    re.IGNORECASE,
)


def _harvest(pattern: re.Pattern[str], content: str, collected: list[str], *, json_escaped: bool = False) -> None:
    """Walk ``pattern`` and append a canonical ``https://host/path`` for each hit."""

    for match in pattern.finditer(content):
        host = match.group(1)
        path = match.group(2)
        if json_escaped:
            # JSON-escaped captures still have the ``\/`` form embedded in the
            # path; unescape so the URL we hand back to the downloader is the
            # same as the one we'd build from any other syntactic variant.
            path = path.replace("\\/", "/")
        collected.append(f"https://{host}{path}")


class LocalLinkRewriter:
    SERVER_SIDE_EXTS = {".php", ".asp", ".aspx", ".jsp", ".cgi", ".pl", ".py"}
    REWRITE_SUFFIXES = {".html", ".htm", ".css", ".js", ".php", ".asp", ".aspx", ".jsp"}

    def rewrite_tree(self, root: Path, concurrency: int, *, collected_urls: list[str] | None = None) -> int:
        """Rewrite every supported file under ``root`` for local browsing.

        When ``collected_urls`` is provided, every original absolute URL the
        rewriter touched is appended to it. The caller can use this to ensure
        each rewritten link actually has a local file behind it (downloading
        the missing ones from the archive).
        """

        files = [path for path in root.rglob("*") if path.suffix.lower() in self.REWRITE_SUFFIXES]
        if not files:
            return 0
        if concurrency <= 1:
            return sum(1 for path in files if self.rewrite_file(path, root, collected_urls=collected_urls))

        # In threaded mode each worker collects into its own list to avoid
        # racing on the shared one; we merge at the end.
        def process(path: Path) -> tuple[int, list[str]]:
            local_bucket: list[str] | None = [] if collected_urls is not None else None
            changed = self.rewrite_file(path, root, collected_urls=local_bucket)
            return (1 if changed else 0, local_bucket or [])

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            results = list(executor.map(process, files))
        if collected_urls is not None:
            for _, bucket in results:
                collected_urls.extend(bucket)
        return sum(count for count, _ in results)

    def rewrite_file(self, file_path: Path, site_root: Path, *, collected_urls: list[str] | None = None) -> bool:
        """Rewrite archived/absolute URLs in a downloaded file to local paths."""

        raw = file_path.read_bytes()
        preferred_encoding = self._detect_meta_charset(raw) if file_path.suffix.lower() in {".html", ".htm", ".php", ".asp"} else None
        content, encoding = decode_with_candidates(raw, preferred_encoding)
        original = content

        content = self.rewrite_html_attribute_urls(content, collected_urls=collected_urls)
        content = self.rewrite_css_urls(content, collected_urls=collected_urls)
        content = self.rewrite_js_urls(content, collected_urls=collected_urls)
        content = self.rewrite_json_escaped_urls(content, collected_urls=collected_urls)

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

    def rewrite_html_attribute_urls(self, content: str, *, collected_urls: list[str] | None = None) -> str:
        # Rewrite Wayback-hosted URLs first so we preserve the original path.
        if collected_urls is not None:
            _harvest(_COLLECT_HTML_WAYBACK, content, collected_urls)
        content = re.sub(
            r"""(\s(?:href|src|action|data-src|data-url)=["'])https?://web\.archive\.org/web/\d+(?:id_)?/(?:https?:)?//[^/]+([^"']*)(["'])""",
            lambda match: f"{match.group(1)}{self.normalize_path_for_local(match.group(2))}{match.group(3)}",
            content,
            flags=re.IGNORECASE,
        )
        # Then rewrite regular absolute (https://, http://) and protocol-relative
        # (//host/path) links into local paths.
        if collected_urls is not None:
            _harvest(_COLLECT_HTML_STD, content, collected_urls)
        return re.sub(
            r"""(\s(?:href|src|action|data-src|data-url)=["'])(?:https?:)?//[^/]+([^"']*)(["'])""",
            lambda match: f"{match.group(1)}{self.normalize_path_for_local(match.group(2))}{match.group(3)}",
            content,
            flags=re.IGNORECASE,
        )

    def rewrite_css_urls(self, content: str, *, collected_urls: list[str] | None = None) -> str:
        # CSS url(...) references appear in archived HTML and in standalone CSS.
        if collected_urls is not None:
            _harvest(_COLLECT_CSS_WAYBACK, content, collected_urls)
        content = re.sub(
            r"""url\(\s*["']?https?://web\.archive\.org/web/\d+(?:id_)?/(?:https?:)?//[^/]+([^"')]*?)["']?\s*\)""",
            lambda match: f'url("{self.normalize_path_for_local(match.group(1))}")',
            content,
            flags=re.IGNORECASE,
        )
        if collected_urls is not None:
            _harvest(_COLLECT_CSS_STD, content, collected_urls)
        return re.sub(
            r"""url\(\s*["']?(?:https?:)?//[^/]+([^"')]*?)["']?\s*\)""",
            lambda match: f'url("{self.normalize_path_for_local(match.group(1))}")',
            content,
            flags=re.IGNORECASE,
        )

    def rewrite_js_urls(self, content: str, *, collected_urls: list[str] | None = None) -> str:
        # JavaScript string literals often embed full absolute URLs.
        if collected_urls is not None:
            _harvest(_COLLECT_JS_WAYBACK, content, collected_urls)
        content = re.sub(
            r"""(["'])https?://web\.archive\.org/web/\d+(?:id_)?/(?:https?:)?//[^/]+([^"']*)(["'])""",
            lambda match: f"{match.group(1)}{self.normalize_path_for_local(match.group(2))}{match.group(3)}",
            content,
            flags=re.IGNORECASE,
        )
        if collected_urls is not None:
            _harvest(_COLLECT_JS_STD, content, collected_urls)
        return re.sub(
            r"""(["'])(?:https?:)?//[^/]+([^"']*)(["'])""",
            lambda match: f"{match.group(1)}{self.normalize_path_for_local(match.group(2))}{match.group(3)}",
            content,
            flags=re.IGNORECASE,
        )

    def rewrite_json_escaped_urls(self, content: str, *, collected_urls: list[str] | None = None) -> str:
        """Rewrite JSON-escaped URLs (``https:\\/\\/host\\/path``).

        WordPress and similar CMSes serialize URLs into inline ``<script>``
        blocks as JSON, which escapes every forward slash. The standard JS
        pass only matches literal ``//``, so these references would otherwise
        survive and force the locally-served page to call out to the live
        host. We preserve the source's escape style by un-escaping the path
        for normalization and re-escaping the slashes in the substitution, so
        the surrounding JSON stays well-formed.
        """

        # Wayback-wrapped variant first so we recover the underlying path.
        if collected_urls is not None:
            _harvest(_COLLECT_JSON_WAYBACK, content, collected_urls, json_escaped=True)
        content = re.sub(
            r"""(["'])https?:\\/\\/web\.archive\.org\\/web\\/\d+(?:id_)?\\/(?:https?:)?\\/\\/[^\\"']+((?:\\/[^"']*?)?)(["'])""",
            self._rewrite_json_escaped_match,
            content,
            flags=re.IGNORECASE,
        )
        if collected_urls is not None:
            _harvest(_COLLECT_JSON_STD, content, collected_urls, json_escaped=True)
        return re.sub(
            r"""(["'])(?:https?:)?\\/\\/[^\\"']+((?:\\/[^"']*?)?)(["'])""",
            self._rewrite_json_escaped_match,
            content,
            flags=re.IGNORECASE,
        )

    def _rewrite_json_escaped_match(self, match: re.Match) -> str:
        unescaped_path = match.group(2).replace("\\/", "/")
        local_path = self.normalize_path_for_local(unescaped_path)
        escaped_local_path = local_path.replace("/", "\\/")
        return f"{match.group(1)}{escaped_local_path}{match.group(3)}"

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
