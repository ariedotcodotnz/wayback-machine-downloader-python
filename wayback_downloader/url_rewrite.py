from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from os import name as os_name
from pathlib import Path
from typing import Iterable

from .paths import _apply_query_digest, sanitize_reference_path
from .text import decode_with_candidates


# Host character class: hostnames don't contain quotes, whitespace, angle
# brackets, parens, or commas. Using a permissive `[^/]+` previously let the
# match eat past the closing quote of an HTML attribute into adjacent markup,
# producing garbage "URLs" like `https://www.googletagmanager.com' /><link rel=`
# that we'd then try (and fail) to download.
_HOST = r"[^/\"'\s<>(),]+"
_JSON_HOST = r"[^\\\"'\s<>(),]+"

# Collection patterns mirror the substitution patterns below but additionally
# capture the host so the caller can reconstruct the original URL. They are
# defined alongside the substitution regexes so future edits to one side are
# obviously paired with the other.
_COLLECT_HTML_WAYBACK = re.compile(
    rf"""\s(?:href|src|action|data-src|data-url)=["']https?://web\.archive\.org/web/\d+(?:id_)?/(?:https?:)?//({_HOST})([^"']*)["']""",
    re.IGNORECASE,
)
_COLLECT_HTML_STD = re.compile(
    rf"""\s(?:href|src|action|data-src|data-url)=["'](?:https?:)?//({_HOST})([^"']*)["']""",
    re.IGNORECASE,
)
_COLLECT_CSS_WAYBACK = re.compile(
    rf"""url\(\s*["']?https?://web\.archive\.org/web/\d+(?:id_)?/(?:https?:)?//({_HOST})([^"')]*?)["']?\s*\)""",
    re.IGNORECASE,
)
_COLLECT_CSS_STD = re.compile(
    rf"""url\(\s*["']?(?:https?:)?//({_HOST})([^"')]*?)["']?\s*\)""",
    re.IGNORECASE,
)
_COLLECT_JS_WAYBACK = re.compile(
    rf"""["']https?://web\.archive\.org/web/\d+(?:id_)?/(?:https?:)?//({_HOST})([^"']*)["']""",
    re.IGNORECASE,
)
_COLLECT_JS_STD = re.compile(
    rf"""["'](?:https?:)?//({_HOST})([^"']*)["']""",
    re.IGNORECASE,
)
_COLLECT_JSON_WAYBACK = re.compile(
    rf"""["']https?:\\/\\/web\.archive\.org\\/web\\/\d+(?:id_)?\\/(?:https?:)?\\/\\/({_JSON_HOST})((?:\\/[^"']*?)?)["']""",
    re.IGNORECASE,
)
_COLLECT_JSON_STD = re.compile(
    rf"""["'](?:https?:)?\\/\\/({_JSON_HOST})((?:\\/[^"']*?)?)["']""",
    re.IGNORECASE,
)

# Matches the entire value of a ``srcset`` attribute so we can split it on
# commas and rewrite each URL individually. Must run before the standard JS
# pass, which would otherwise capture the whole comma-joined value as a single
# garbage URL.
_SRCSET_ATTR = re.compile(
    r"""(\s(?:srcset|data-srcset|imagesrcset)\s*=\s*["'])([^"']+)(["'])""",
    re.IGNORECASE,
)
_SRCSET_DESCRIPTOR = re.compile(r"^\d+(?:\.\d+)?[wx]$", re.IGNORECASE)
_ABSOLUTE_OR_PROTO_REL = re.compile(rf"^(?:https?:)?//({_HOST})(.*)$", re.IGNORECASE)

# Matches a CSS ``url(...)`` whose argument is a relative URL with a query
# string (e.g. ``url("fonts/icon.woff?v=4.2")``). The downloader folds query
# strings into the filename (saving as ``icon__q<hash>.woff``), but a relative
# reference like this is invisible to the existing absolute-URL passes, so the
# browser ends up asking for the literal ``icon.woff?v=4.2`` URL and 404s.
# This pass rewrites the reference to match the on-disk filename.
#
# The query is required to start with an alphanumeric character (or ``_``),
# which excludes JS optional chaining (``obj?.prop``, ``arr?.[i]``, ``fn?.()``).
# Without this, case-insensitive ``url\(`` matched ``URL(`` inside identifiers
# like ``compareURL(`` and folded the JS expression into a ``__q<hash>``
# filename — silently corrupting minified JS that used optional chaining.
_CSS_RELATIVE_QUERY = re.compile(
    r"""(url\(\s*["']?)((?!data:|https?:|//)[^"')\s]+\?[a-zA-Z0-9_][^"')\s]*)(["']?\s*\))""",
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

        # Computed once per file; threaded into every rewrite method so that
        # absolute URLs in deep files use the correct number of ``../`` hops
        # back to the site root, not a naive ``./`` that resolves to the
        # file's own directory.
        root_prefix = self.site_root_relative_prefix(file_path, site_root)

        # Gate passes by file type so a regex that's safe in one context
        # doesn't run in another where it can produce corruption. For
        # example, CSS ``url\(`` is case-insensitive and matches ``URL(``
        # inside identifiers like ``compareURL(``; running the CSS pass on
        # a minified JS file with optional chaining (``obj?.prop``) silently
        # rewrites the JS expression into a ``__q<hash>`` filename.
        suffix = file_path.suffix.lower()
        is_html_like = suffix in {".html", ".htm", ".php", ".asp", ".aspx", ".jsp"}
        is_css = suffix == ".css"
        is_js = suffix == ".js"

        if is_html_like:
            # srcset must run first: it splits the comma-joined value into
            # individual URLs and rewrites each one, so subsequent passes
            # don't accidentally match the entire srcset attribute value as
            # one URL.
            content = self.rewrite_srcset_urls(content, root_prefix=root_prefix, collected_urls=collected_urls)
            content = self.rewrite_html_attribute_urls(content, root_prefix=root_prefix, collected_urls=collected_urls)
        if is_html_like or is_css:
            content = self.rewrite_css_urls(content, root_prefix=root_prefix, collected_urls=collected_urls)
            content = self.rewrite_css_relative_query_urls(content)
        if is_html_like or is_js:
            content = self.rewrite_js_urls(content, root_prefix=root_prefix, collected_urls=collected_urls)
            content = self.rewrite_json_escaped_urls(content, root_prefix=root_prefix, collected_urls=collected_urls)

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

    def rewrite_srcset_urls(self, content: str, *, root_prefix: str = "./", collected_urls: list[str] | None = None) -> str:
        """Rewrite each URL in ``srcset`` attribute values individually.

        A srcset value looks like ``url1 300w, url2 768w, url3 1536w``. The
        previous code had no srcset-specific pass, so the standard JS regex
        captured the whole comma-joined value as one quoted URL — producing
        bogus download requests like ``https://host/foo.jpg 2000w, https://...``.
        This pass splits the value, rewrites each absolute URL to a local
        path (preserving the size descriptor), and reassembles.
        """

        def replace(match: re.Match) -> str:
            prefix, value, suffix = match.group(1), match.group(2), match.group(3)
            new_parts: list[str] = []
            for raw_part in value.split(","):
                part = raw_part.strip()
                if not part:
                    continue
                url, descriptor = self._split_srcset_part(part)
                if collected_urls is not None:
                    abs_match = _ABSOLUTE_OR_PROTO_REL.match(url)
                    if abs_match:
                        collected_urls.append(f"https://{abs_match.group(1)}{abs_match.group(2)}")
                new_url = self._rewrite_absolute_to_local(url, root_prefix)
                new_parts.append(f"{new_url} {descriptor}".strip())
            return f"{prefix}{', '.join(new_parts)}{suffix}"

        return _SRCSET_ATTR.sub(replace, content)

    @staticmethod
    def _split_srcset_part(part: str) -> tuple[str, str]:
        """Return ``(url, descriptor)`` for one srcset part; descriptor may be empty."""

        if " " not in part:
            return part, ""
        url, candidate = part.rsplit(" ", 1)
        if _SRCSET_DESCRIPTOR.match(candidate):
            return url.strip(), candidate
        return part, ""

    def _rewrite_absolute_to_local(self, url: str, root_prefix: str) -> str:
        """Rewrite an absolute or protocol-relative URL to a local path; leave others alone."""

        match = _ABSOLUTE_OR_PROTO_REL.match(url)
        if not match:
            return url
        return self._local_path_for(match.group(2), root_prefix)

    def rewrite_html_attribute_urls(self, content: str, *, root_prefix: str = "./", collected_urls: list[str] | None = None) -> str:
        # Rewrite Wayback-hosted URLs first so we preserve the original path.
        if collected_urls is not None:
            _harvest(_COLLECT_HTML_WAYBACK, content, collected_urls)
        content = re.sub(
            rf"""(\s(?:href|src|action|data-src|data-url)=["'])https?://web\.archive\.org/web/\d+(?:id_)?/(?:https?:)?//{_HOST}([^"']*)(["'])""",
            lambda match: f"{match.group(1)}{self._local_path_for(match.group(2), root_prefix)}{match.group(3)}",
            content,
            flags=re.IGNORECASE,
        )
        # Then rewrite regular absolute (https://, http://) and protocol-relative
        # (//host/path) links into local paths.
        if collected_urls is not None:
            _harvest(_COLLECT_HTML_STD, content, collected_urls)
        return re.sub(
            rf"""(\s(?:href|src|action|data-src|data-url)=["'])(?:https?:)?//{_HOST}([^"']*)(["'])""",
            lambda match: f"{match.group(1)}{self._local_path_for(match.group(2), root_prefix)}{match.group(3)}",
            content,
            flags=re.IGNORECASE,
        )

    def rewrite_css_urls(self, content: str, *, root_prefix: str = "./", collected_urls: list[str] | None = None) -> str:
        # CSS url(...) references appear in archived HTML and in standalone CSS.
        # Capture the opening and closing quote (or its absence) so the
        # substitution preserves them. Hardcoding ``url("...")`` previously
        # broke HTML ``style="..."`` attributes: a substituted ``"`` closed
        # the style attribute early and the rest of the URL was parsed as
        # more attributes.
        if collected_urls is not None:
            _harvest(_COLLECT_CSS_WAYBACK, content, collected_urls)
        content = re.sub(
            rf"""url\(\s*(["']?)https?://web\.archive\.org/web/\d+(?:id_)?/(?:https?:)?//{_HOST}([^"')]*?)(["']?)\s*\)""",
            lambda match: f"url({match.group(1)}{self._local_path_for(match.group(2), root_prefix)}{match.group(3)})",
            content,
            flags=re.IGNORECASE,
        )
        if collected_urls is not None:
            _harvest(_COLLECT_CSS_STD, content, collected_urls)
        return re.sub(
            rf"""url\(\s*(["']?)(?:https?:)?//{_HOST}([^"')]*?)(["']?)\s*\)""",
            lambda match: f"url({match.group(1)}{self._local_path_for(match.group(2), root_prefix)}{match.group(3)})",
            content,
            flags=re.IGNORECASE,
        )

    def rewrite_js_urls(self, content: str, *, root_prefix: str = "./", collected_urls: list[str] | None = None) -> str:
        # JavaScript string literals often embed full absolute URLs that are
        # used as base URLs for concatenation (``base + "subpath"``). Use
        # ``as_base_url=True`` so a trailing slash on the source URL is
        # preserved and we don't append ``/index.html``.
        if collected_urls is not None:
            _harvest(_COLLECT_JS_WAYBACK, content, collected_urls)
        content = re.sub(
            rf"""(["'])https?://web\.archive\.org/web/\d+(?:id_)?/(?:https?:)?//{_HOST}([^"']*)(["'])""",
            lambda match: f"{match.group(1)}{self._local_path_for(match.group(2), root_prefix, as_base_url=True)}{match.group(3)}",
            content,
            flags=re.IGNORECASE,
        )
        if collected_urls is not None:
            _harvest(_COLLECT_JS_STD, content, collected_urls)
        return re.sub(
            rf"""(["'])(?:https?:)?//{_HOST}([^"']*)(["'])""",
            lambda match: f"{match.group(1)}{self._local_path_for(match.group(2), root_prefix, as_base_url=True)}{match.group(3)}",
            content,
            flags=re.IGNORECASE,
        )

    def rewrite_css_relative_query_urls(self, content: str) -> str:
        """Fold ``?query`` into the filename for *relative* CSS ``url()`` refs.

        The downloader saves ``foo.woff?v=4.2`` as ``foo__q<hash>.woff`` on
        disk. The absolute-URL CSS pass handles cases like
        ``url("https://host/foo.woff?v=4.2")``, but bare relative references
        like ``url("fonts/foo.woff?v=4.2")`` are invisible to it and produce
        font 404s. This pass rewrites just the query-bearing relative refs,
        preserving the directory structure and only changing the filename.
        """

        def replace(match: re.Match) -> str:
            prefix, url, suffix = match.group(1), match.group(2), match.group(3)
            path_part, _, query_part = url.partition("?")
            if not query_part:
                return match.group(0)
            folded = _apply_query_digest(path_part, query_part)
            return f"{prefix}{folded}{suffix}"

        return _CSS_RELATIVE_QUERY.sub(replace, content)

    def rewrite_json_escaped_urls(self, content: str, *, root_prefix: str = "./", collected_urls: list[str] | None = None) -> str:
        """Rewrite JSON-escaped URLs (``https:\\/\\/host\\/path``).

        WordPress and similar CMSes serialize URLs into inline ``<script>``
        blocks as JSON, which escapes every forward slash. The standard JS
        pass only matches literal ``//``, so these references would otherwise
        survive and force the locally-served page to call out to the live
        host. We preserve the source's escape style by un-escaping the path
        for normalization and re-escaping the slashes in the substitution, so
        the surrounding JSON stays well-formed.
        """

        def replace(match: re.Match) -> str:
            unescaped_path = match.group(2).replace("\\/", "/")
            # JSON-escaped URLs almost always live in script blocks where the
            # value is used as a JS base URL — same as ``rewrite_js_urls``,
            # we preserve the trailing slash and skip the ``/index.html``
            # appendage.
            local_path = self._local_path_for(unescaped_path, root_prefix, as_base_url=True)
            escaped_local_path = local_path.replace("/", "\\/")
            return f"{match.group(1)}{escaped_local_path}{match.group(3)}"

        # Wayback-wrapped variant first so we recover the underlying path.
        if collected_urls is not None:
            _harvest(_COLLECT_JSON_WAYBACK, content, collected_urls, json_escaped=True)
        content = re.sub(
            rf"""(["'])https?:\\/\\/web\.archive\.org\\/web\\/\d+(?:id_)?\\/(?:https?:)?\\/\\/{_JSON_HOST}((?:\\/[^"']*?)?)(["'])""",
            replace,
            content,
            flags=re.IGNORECASE,
        )
        if collected_urls is not None:
            _harvest(_COLLECT_JSON_STD, content, collected_urls, json_escaped=True)
        return re.sub(
            rf"""(["'])(?:https?:)?\\/\\/{_JSON_HOST}((?:\\/[^"']*?)?)(["'])""",
            replace,
            content,
            flags=re.IGNORECASE,
        )

    def _local_path_for(self, path: str, root_prefix: str, *, as_base_url: bool = False) -> str:
        """Swap the leading ``./`` from ``normalize_path_for_local`` for the file's actual depth-relative prefix.

        ``normalize_path_for_local`` always returns ``./xxx`` (treating the
        result as if the file lived at the site root). For files in
        subdirectories, the browser resolves ``./xxx`` against the file's own
        directory, which produces a wrong path. We substitute the right number
        of ``../`` hops here so the resolved URL actually points at the
        downloaded file. See ``normalize_path_for_local`` for ``as_base_url``.
        """

        local = self.normalize_path_for_local(path, as_base_url=as_base_url)
        if local.startswith("./"):
            return root_prefix + local[2:]
        return local

    def normalize_path_for_local(self, path: str, *, as_base_url: bool = False) -> str:
        """Convert an archived absolute path into the downloaded local path.

        Query strings are folded into the filename using the same
        ``__q<digest>`` convention that the downloader uses on disk, so
        rewritten references still point at the saved file.

        Pass ``as_base_url=True`` when the path is being used as a JS base
        URL (the most common case for JSON-embedded URLs and string literals
        used in ``base + "subpath"`` patterns). In that mode we preserve a
        trailing slash and skip the ``/index.html`` appendage so
        concatenation produces a valid URL — without this the WordPress
        REST endpoint ``"/wp-json/"`` would become ``"./wp-json/index.html"``
        and ``endpoint + "wp/v2/users/me"`` would produce
        ``"./wp-json/index.htmlwp/v2/users/me"``.
        """

        if not path or path == "/":
            return "./" if as_base_url else "./index.html"

        normalized_tail = sanitize_reference_path(path, filesystem_safe=os_name == "nt")
        if not normalized_tail:
            return "./" if as_base_url else "./index.html"

        relative_path = f"./{normalized_tail}"
        if as_base_url:
            if path.endswith("/") and not relative_path.endswith("/"):
                relative_path += "/"
            return relative_path

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
