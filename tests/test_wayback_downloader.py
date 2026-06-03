from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from wayback_downloader.archive import ArchiveClient
from wayback_downloader.cli import build_config, build_parser
from wayback_downloader.config import DownloadConfig
from wayback_downloader.downloader import WaybackDownloader
from wayback_downloader.filters import URLFilter
from wayback_downloader.models import FetchDisposition, FetchResult, HTTPResponse, Snapshot
from wayback_downloader.paths import LocalPathMapper
from wayback_downloader.requisites import PageRequisitesExtractor
from wayback_downloader.snapshots import SnapshotPlanner
from wayback_downloader.text import repeated_percent_decode
from wayback_downloader.url_rewrite import LocalLinkRewriter


class FakeTransport:
    def __init__(self, responses: dict[str, list[HTTPResponse] | HTTPResponse]) -> None:
        self.responses = {
            key: value if isinstance(value, list) else [value]
            for key, value in responses.items()
        }
        self.seen: list[str] = []

    def get(self, url: str, headers=None) -> HTTPResponse:
        self.seen.append(url)
        response_list = self.responses.get(url)
        if not response_list:
            raise AssertionError(f"No fake response registered for {url}")
        return response_list.pop(0)

    def close(self) -> None:
        return None


class URLFilterTests(unittest.TestCase):
    def test_literal_and_regex_filters(self) -> None:
        matcher = URLFilter(include_pattern="/\\.(gif|png)$/i", exclude_pattern="logo")
        self.assertTrue(matcher.matches_include("https://example.com/image.GIF"))
        self.assertFalse(matcher.allows("https://example.com/logo.gif"))
        self.assertFalse(matcher.allows("https://example.com/index.html"))


class SnapshotPlannerTests(unittest.TestCase):
    def test_latest_all_timestamps_and_composite_modes(self) -> None:
        mapper = LocalPathMapper(Path("unused"))
        planner = SnapshotPlanner(URLFilter(), mapper)
        raw = [
            (20200101000000, "http://example.com/index.html"),
            (20200102000000, "http://example.com/index.html"),
            (20200101595959, "http://example.com/app.js"),
        ]

        latest = planner.build(raw, all_timestamps=False, snapshot_at=None)
        self.assertEqual([snapshot.file_id for snapshot in latest], ["index.html", "app.js"])
        self.assertEqual(latest[0].timestamp, 20200102000000)

        all_timestamps = planner.build(raw, all_timestamps=True, snapshot_at=None)
        self.assertEqual(len(all_timestamps), 3)
        self.assertTrue(all(snapshot.file_id.startswith("202001") for snapshot in all_timestamps))

        composite = planner.build(raw, all_timestamps=False, snapshot_at=20200101595959)
        self.assertEqual(len(composite), 2)
        self.assertEqual(composite[0].timestamp, 20200101595959)

    def test_query_strings_are_hashed_into_file_ids(self) -> None:
        mapper = LocalPathMapper(Path("unused"))
        sanitized = mapper.sanitize_file_id("search?q=test", "http://example.com/search?q=test")
        self.assertIn("__q", sanitized)


class ArchiveClientTests(unittest.TestCase):
    def test_default_parameters_match_ruby_behavior(self) -> None:
        config = DownloadConfig(target="https://example.com")
        client = ArchiveClient(config, transport=FakeTransport({}))
        parameters = dict(client.parameters_for_api(0))
        self.assertEqual(parameters["filter"], "statuscode:2..|30[12378]")
        self.assertEqual(parameters["collapse"], "digest")

    def test_download_capture_follows_relative_redirects(self) -> None:
        config = DownloadConfig(target="https://example.com")
        client = ArchiveClient(config, transport=FakeTransport({}))
        first_url = client.build_wayback_url("http://www.example.com/index.php", 20200101000000)
        second_url = client.build_wayback_url("http://www.example.com/new-path", 20200101000000)
        client.transport = FakeTransport(
            {
                first_url: HTTPResponse(302, "Found", {"location": "/new-path"}, b""),
                second_url: HTTPResponse(200, "OK", {}, b"redirected content"),
            }
        )

        result = client.download_capture("http://www.example.com/index.php", 20200101000000)

        self.assertEqual(result.body, b"redirected content")
        self.assertEqual(client.transport.seen, [first_url, second_url])

    def test_download_capture_keeps_raw_body_when_gzip_header_is_wrong(self) -> None:
        config = DownloadConfig(target="https://example.com", max_retries=0)
        client = ArchiveClient(config, transport=FakeTransport({}))
        request_url = client.build_wayback_url("http://www.example.com/bad.gz", 20200101000000)
        client.transport = FakeTransport(
            {
                request_url: HTTPResponse(200, "OK", {"content-encoding": "gzip"}, b"not really gzip"),
            }
        )

        result = client.download_capture("http://www.example.com/bad.gz", 20200101000000)

        self.assertEqual(result.body, b"not really gzip")

    def test_normalize_query_url_adds_wildcards_for_root_and_directory_targets(self) -> None:
        client = ArchiveClient(DownloadConfig(target="https://example.com"), transport=FakeTransport({}))
        self.assertEqual(client.normalize_query_url("https://example.com/"), "https://example.com/*")
        self.assertEqual(client.normalize_query_url("example.com"), "example.com/*")
        self.assertEqual(client.normalize_query_url("https://example.com/wiki/"), "https://example.com/wiki/*")
        self.assertEqual(client.normalize_query_url("https://example.com/wiki/page.html"), "https://example.com/wiki/page.html")


class CliTests(unittest.TestCase):
    def test_cross_host_flag_reaches_runtime_config(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--cross-host", "https://example.com"])

        config = build_config(args)

        self.assertTrue(config.cross_host)


class RepeatedPercentDecodeTests(unittest.TestCase):
    def test_ascii_without_escapes_is_unchanged(self) -> None:
        self.assertEqual(repeated_percent_decode("plain/path"), b"plain/path")

    def test_single_utf8_percent_escape_unwraps(self) -> None:
        self.assertEqual(repeated_percent_decode("caf%C3%A9"), "café".encode("utf-8"))

    def test_double_percent_escape_unwraps_twice(self) -> None:
        self.assertEqual(repeated_percent_decode("caf%25C3%25A9"), "café".encode("utf-8"))

    def test_raw_non_ascii_terminates_without_growing(self) -> None:
        # This is the regression case: the previous implementation grew the
        # working string on every iteration when given non-ASCII input and
        # eventually triggered MemoryError. The fix should return immediately.
        self.assertEqual(repeated_percent_decode("café"), "café".encode("utf-8"))

    def test_legacy_byte_sequence_is_returned_verbatim(self) -> None:
        # cp1251-encoded "привет" percent-escaped should come out as raw bytes
        # so the caller can sniff the right encoding via decode_best_effort.
        self.assertEqual(
            repeated_percent_decode("%EF%F0%E8%E2%E5%F2"),
            b"\xef\xf0\xe8\xe2\xe5\xf2",
        )


class RewriteTests(unittest.TestCase):
    def test_page_requisite_extraction_supports_srcset(self) -> None:
        html = '<img srcset="one.jpg 1x, two.jpg 2x"><script src="app.js"></script>'
        self.assertEqual(PageRequisitesExtractor.extract(html), ["one.jpg", "two.jpg", "app.js"])

    def test_local_rewriter_rewrites_root_absolute_urls(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            file_path = root / "foo" / "bar" / "index.html"
            file_path.parent.mkdir(parents=True)
            file_path.write_text('<img src="/img/logo.png"><style>body{background:url("/css/bg.png")}</style>', encoding="utf-8")

            rewriter = LocalLinkRewriter()
            rewritten = rewriter.rewrite_file(file_path, root)
            content = file_path.read_text(encoding="utf-8")

            self.assertTrue(rewritten)
            self.assertIn('src="../../img/logo.png"', content)
            self.assertIn('url("../../css/bg.png")', content)

    def test_local_rewriter_preserves_query_hashed_filenames(self) -> None:
        rewriter = LocalLinkRewriter()
        rewritten = rewriter.normalize_path_for_local("/assets/app.css?version=123")
        self.assertRegex(rewritten, r"^\./assets/app__q[0-9a-f]{12}\.css$")

    def test_protocol_relative_html_attribute_is_rewritten(self) -> None:
        rewriter = LocalLinkRewriter()
        # The user's real-world case: WordPress emits <link href='//host/...'>
        # which the previous rewriter ignored because the scheme was missing.
        html = " href='//voteforit.nz/wp-content/plugins/foo/style.css?ver=1.0'"
        rewritten = rewriter.rewrite_html_attribute_urls(html)
        self.assertNotIn("voteforit.nz", rewritten)
        self.assertRegex(rewritten, r"href='\./wp-content/plugins/foo/style__q[0-9a-f]{12}\.css'")

    def test_json_escaped_url_is_rewritten_with_preserved_escapes(self) -> None:
        rewriter = LocalLinkRewriter()
        content = '"concatemoji":"https:\\/\\/voteforit.nz\\/wp-includes\\/js\\/wp-emoji-release.min.js?ver=6.8.3"'
        rewritten = rewriter.rewrite_json_escaped_urls(content)

        self.assertNotIn("voteforit.nz", rewritten)
        # The substituted path is a local relative path with slashes escaped
        # the same way as the source JSON so the surrounding literal stays
        # well-formed.
        self.assertRegex(
            rewritten,
            r'"concatemoji":"\.\\/wp-includes\\/js\\/wp-emoji-release\.min__q[0-9a-f]{12}\.js"',
        )
        # Round-trip safety: the rewritten string should still be valid JSON.
        import json as _json
        decoded = _json.loads("{" + rewritten + "}")
        self.assertTrue(decoded["concatemoji"].startswith("./wp-includes/"))

    def test_absolute_url_in_nested_file_uses_depth_relative_prefix(self) -> None:
        # Regression: the rewriter used to emit ``./wp-includes/...`` for
        # absolute URLs regardless of file depth. In a file at
        # ``foo/bar/index.html`` the browser resolved that against the file's
        # directory and asked for ``foo/bar/wp-includes/...`` (404 locally).
        # Worse, the page-requisites extractor did the same urljoin and
        # queued phantom URLs like ``https://host/foo/bar/wp-includes/...``
        # for download, which produced thousands of NOT FOUND errors.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            file_path = root / "foo" / "bar" / "index.html"
            file_path.parent.mkdir(parents=True)
            file_path.write_text(
                '<link href="https://voteforit.nz/wp-includes/style.css">'
                '<script src="https://voteforit.nz/wp-includes/js/wp.js"></script>'
                '<style>.bg { background: url("https://voteforit.nz/img/bg.png"); }</style>',
                encoding="utf-8",
            )

            rewriter = LocalLinkRewriter()
            rewriter.rewrite_file(file_path, root)
            content = file_path.read_text(encoding="utf-8")

            self.assertIn('href="../../wp-includes/style.css"', content)
            self.assertIn('src="../../wp-includes/js/wp.js"', content)
            self.assertIn('url("../../img/bg.png")', content)
            self.assertNotIn('href="./wp-includes/', content)
            self.assertNotIn('src="./wp-includes/', content)

    def test_js_string_with_trailing_slash_preserves_base_url_shape(self) -> None:
        # Regression: a JS object had ``endpoint: "https://host/wp-json/"`` —
        # used as a base for WordPress REST concatenation. The rewriter
        # previously turned the trailing slash into ``/index.html``, so
        # ``endpoint + "wp/v2/users/me"`` produced
        # ``"./wp-json/index.htmlwp/v2/users/me"`` — broken. The fix
        # preserves the trailing slash in JS-context rewrites so
        # concatenation still produces a valid URL.
        rewriter = LocalLinkRewriter()
        js = '{"endpoint":"https://voteforit.nz/wp-json/"}'
        rewritten = rewriter.rewrite_js_urls(js)
        self.assertIn('"endpoint":"./wp-json/"', rewritten)
        self.assertNotIn("index.html", rewritten)

    def test_json_escaped_trailing_slash_preserves_base_url_shape(self) -> None:
        # Same as above but for JSON-escaped script-block configs (the form
        # WordPress's inline settings most often use).
        rewriter = LocalLinkRewriter()
        content = '"endpoint":"https:\\/\\/voteforit.nz\\/wp-json\\/"'
        rewritten = rewriter.rewrite_json_escaped_urls(content)
        self.assertIn('"endpoint":".\\/wp-json\\/"', rewritten)
        self.assertNotIn("index.html", rewritten)

    def test_html_attribute_trailing_slash_still_becomes_index_html(self) -> None:
        # Counter-test: the JS fix must NOT bleed into HTML attribute
        # rewriting. ``<a href="https://host/about/">`` should still rewrite
        # to ``./about/index.html`` (because the browser navigates to that
        # URL as a document, not as a base for concatenation).
        rewriter = LocalLinkRewriter()
        html = '<a href="https://voteforit.nz/about/">About</a>'
        rewritten = rewriter.rewrite_html_attribute_urls(html)
        self.assertIn('href="./about/index.html"', rewritten)

    def test_css_relative_url_with_query_is_folded_to_disk_filename(self) -> None:
        # Regression: CSS files like ``font-awesome-legacy.min.css`` contain
        # references like ``url("fonts/fontawesome-webfont.woff?v=4.2")``.
        # The downloader saves the file as ``fontawesome-webfont__q<hash>.woff``
        # (query folded into filename), but the relative reference was left
        # untouched by the rewriter, so the browser asked for the literal
        # ``?v=4.2`` URL and 404'd. This pass folds the query into the
        # filename so the on-disk path is what the browser asks for.
        rewriter = LocalLinkRewriter()
        css = '@font-face { src: url("fonts/fontawesome-webfont.woff?v=4.2") format("woff"); }'
        rewritten = rewriter.rewrite_css_relative_query_urls(css)
        # Reference no longer has the literal ``?v=4.2``…
        self.assertNotIn("?v=4.2", rewritten)
        # …and matches the ``__q<hash>`` folded form the downloader uses.
        self.assertRegex(rewritten, r'url\("fonts/fontawesome-webfont__q[0-9a-f]{12}\.woff"\)')

    def test_css_relative_query_does_not_corrupt_js_optional_chaining(self) -> None:
        # Regression: case-insensitive ``url\(`` matched ``URL(`` inside
        # identifiers like ``compareURL(``, and ``?.`` optional chaining
        # satisfied the ``\?...`` shape. The pass then folded the JS
        # expression into a ``__q<hash>`` filename — silently corrupting
        # minified JS that used optional chaining. The fix requires the
        # query to start with an alphanumeric character.
        rewriter = LocalLinkRewriter()
        js_with_optional_chaining = (
            "_tpt.compareURL(SR7.M[e].imgList[t]?.old, i) && doThing(arr?.[0]?.value)"
        )
        rewritten = rewriter.rewrite_css_relative_query_urls(js_with_optional_chaining)
        # Must be byte-for-byte unchanged.
        self.assertEqual(rewritten, js_with_optional_chaining)

    def test_css_url_quote_style_is_preserved_in_html_style_attribute(self) -> None:
        # Regression: substituting ``url("path")`` (double quotes) inside an
        # HTML ``style="..."`` attribute closes the outer attribute early,
        # so ``<div style="background-image: url(https://host/foo.jpg)">``
        # was corrupted into ``<div style="background-image: url("
        # wp-content="" uploads="" 04="" foo.jpg")="">``. Preserve the
        # original quote style (or absence of quotes) so the substitution
        # doesn't break HTML attribute parsing.
        rewriter = LocalLinkRewriter()
        html = '<div style="background-image: url(https://voteforit.nz/img/bg.jpg)"></div>'
        rewritten = rewriter.rewrite_css_urls(html)
        # No inner quotes injected — the source had none.
        self.assertIn('url(./img/bg.jpg)', rewritten)
        self.assertNotIn('url("./img/bg.jpg")', rewritten)

    def test_css_url_quote_style_preserves_single_quotes(self) -> None:
        rewriter = LocalLinkRewriter()
        css = "body { background: url('https://voteforit.nz/img/bg.jpg'); }"
        rewritten = rewriter.rewrite_css_urls(css)
        self.assertIn("url('./img/bg.jpg')", rewritten)

    def test_css_url_quote_style_preserves_double_quotes_in_css(self) -> None:
        # In a standalone CSS file (where the outer container isn't a
        # double-quoted HTML attribute), double quotes are fine and must
        # be preserved if that's what the source used.
        rewriter = LocalLinkRewriter()
        css = 'body { background: url("https://voteforit.nz/img/bg.jpg"); }'
        rewritten = rewriter.rewrite_css_urls(css)
        self.assertIn('url("./img/bg.jpg")', rewritten)

    def test_css_pass_does_not_run_on_js_files(self) -> None:
        # Defense in depth: CSS passes should be file-gated to .css and HTML-
        # like files. Even with the regex-tightening above, running CSS
        # rewrites on .js files is a recipe for collisions between
        # JS syntax and CSS-shaped patterns. This test feeds a .js file
        # containing both legitimate JS optional chaining AND text that
        # would match CSS rewrites if the pass ran — only the JS passes
        # should touch it.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            js_file = root / "sr7.js"
            js_source = (
                "var compareURL = function(a, b) { "
                "return SR7.M[e].imgList[t]?.old === b; "
                "};\n"
                # A literal HTTPS URL in a JS string SHOULD be rewritten
                # by the JS pass (which is still wired up for .js files).
                "var endpoint = 'https://voteforit.nz/api/foo.json?v=1';"
            )
            js_file.write_text(js_source, encoding="utf-8")

            rewriter = LocalLinkRewriter()
            rewriter.rewrite_file(js_file, root)
            after = js_file.read_text(encoding="utf-8")

            # Optional chaining survives — CSS pass never ran.
            self.assertIn("imgList[t]?.old", after)
            # JS pass DID run — the URL got rewritten.
            self.assertIn("./api/foo__q", after)

    def test_css_absolute_url_with_query_unaffected_by_relative_pass(self) -> None:
        # Counter-test: absolute URLs already go through the existing
        # absolute-CSS pass (which calls _local_path_for / sanitize_reference_path),
        # so the new relative pass must NOT match them — otherwise we'd
        # double-fold and produce a broken filename. The negative lookahead
        # in _CSS_RELATIVE_QUERY enforces this.
        rewriter = LocalLinkRewriter()
        css = '@font-face { src: url("https://example.com/foo.woff?v=1") format("woff"); }'
        rewritten = rewriter.rewrite_css_relative_query_urls(css)
        # Untouched: the absolute pass owns this URL.
        self.assertEqual(rewritten, css)

    def test_srcset_rewriter_matches_lazy_load_data_variants(self) -> None:
        # Regression: Nectar's lazy-load attribute ``data-nectar-img-srcset``
        # was not in the srcset attribute whitelist (which previously only
        # had ``srcset|data-srcset|imagesrcset``). The fallback JS pattern
        # then grabbed the entire value as one URL and mangled it through
        # ``sanitize_reference_path``, producing output like
        # ``https%3A/voteforit.nz/...`` for all URLs past the first.
        rewriter = LocalLinkRewriter()
        html = (
            '<img data-nectar-img-srcset="'
            'https://voteforit.nz/img/a.jpg 1024w, '
            'https://voteforit.nz/img/b.jpg 768w, '
            'https://voteforit.nz/img/c.jpg 300w">'
        )
        rewritten = rewriter.rewrite_srcset_urls(html)
        # All three URLs got rewritten — no ``https%3A/`` corruption.
        self.assertNotIn("https%3A", rewritten)
        self.assertIn("./img/a.jpg 1024w", rewritten)
        self.assertIn("./img/b.jpg 768w", rewritten)
        self.assertIn("./img/c.jpg 300w", rewritten)

    def test_srcset_rewriter_matches_arbitrary_data_prefix(self) -> None:
        # Defense in depth: any CMS-invented data-*srcset variant should
        # be caught by the broadened attribute pattern.
        rewriter = LocalLinkRewriter()
        # Real-world variants: WordPress core, popular lazy-load plugins,
        # Nectar's theme-specific attribute, and HTML5's imagesrcset.
        for attr in ("data-srcset", "data-lazy-srcset", "data-nectar-img-srcset",
                     "data-some-plugins-image-srcset", "imagesrcset"):
            html = f'<img {attr}="https://host/a.jpg 1x, https://host/b.jpg 2x">'
            rewritten = rewriter.rewrite_srcset_urls(html)
            self.assertNotIn("https://host", rewritten, msg=f"{attr} not rewritten")
            self.assertIn("./a.jpg 1x", rewritten)
            self.assertIn("./b.jpg 2x", rewritten)

    def test_js_pattern_refuses_to_slurp_srcset_style_value(self) -> None:
        # Regression: even if a srcset variant slips past the srcset pass,
        # the standard JS pattern must not match a quoted srcset-style
        # value (which has spaces between URL and descriptor). With the
        # old ``[^"']*`` path class, the JS pattern would grab the whole
        # value as one URL and mangle it. With the new ``[^"'\s]*`` class,
        # the path stops at the first space and the overall pattern fails
        # to match (no closing quote where expected).
        rewriter = LocalLinkRewriter()
        # Use an attribute the srcset pass doesn't know about, so we test
        # the JS-pattern fallback in isolation.
        content = 'data-mystery="https://voteforit.nz/a.jpg 1024w, https://voteforit.nz/b.jpg 768w"'
        rewritten = rewriter.rewrite_js_urls(content)
        # Untouched — neither URL was matched, no corruption.
        self.assertEqual(rewritten, content)

    def test_srcset_rewriter_splits_wordpress_responsive_images(self) -> None:
        # Regression: a WordPress srcset value with width descriptors
        # (``2000w``, ``768w``, etc.) used to be captured as one giant URL
        # by the standard JS pass, producing bogus download requests like
        # ``https://host/foo.jpg 2000w, https://host/foo-768w.jpg 768w``.
        # The dedicated srcset pass must split on commas, rewrite each URL
        # to a local path, and preserve the descriptors.
        rewriter = LocalLinkRewriter()
        html = (
            '<img srcset="https://voteforit.nz/img/photo.jpg 2000w, '
            'https://voteforit.nz/img/photo-768x500.jpg 768w, '
            'https://voteforit.nz/img/photo-300x200.jpg 300w">'
        )
        collected: list[str] = []
        rewritten = rewriter.rewrite_srcset_urls(html, collected_urls=collected)

        # Each URL got rewritten independently and the descriptors survived.
        self.assertIn("./img/photo.jpg 2000w", rewritten)
        self.assertIn("./img/photo-768x500.jpg 768w", rewritten)
        self.assertIn("./img/photo-300x200.jpg 300w", rewritten)
        self.assertNotIn("voteforit.nz", rewritten)

        # And the collected URLs are individual canonical URLs, not one blob.
        self.assertEqual(
            sorted(collected),
            sorted([
                "https://voteforit.nz/img/photo.jpg",
                "https://voteforit.nz/img/photo-768x500.jpg",
                "https://voteforit.nz/img/photo-300x200.jpg",
            ]),
        )

    def test_page_requisites_extractor_splits_wordpress_srcset(self) -> None:
        # The extractor's old heuristic only triggered on ` 1x` or ` 2w`.
        # WordPress uses width descriptors like 300w, 768w, 1024w, 2000w —
        # none of which matched, so the whole srcset value was returned as a
        # single bogus asset URL.
        html = (
            '<img srcset="https://host/a.jpg 2000w, '
            'https://host/b.jpg 1024w, '
            'https://host/c.jpg 300w">'
        )
        assets = PageRequisitesExtractor.extract(html)
        self.assertEqual(
            assets,
            ["https://host/a.jpg", "https://host/b.jpg", "https://host/c.jpg"],
        )

    def test_host_regex_stops_at_quotes_and_whitespace(self) -> None:
        # Regression: with the old ``[^/]+`` host pattern, a closing quote
        # immediately after the host let the host class swallow the quote
        # plus any following text up to the next ``/``. Captured "URLs"
        # like ``https://www.googletagmanager.com' /><link rel=`` then got
        # queued for download. The tightened host pattern must stop at
        # the closing quote so the URL is collected cleanly.
        rewriter = LocalLinkRewriter()
        html = (
            "<script src='https://www.googletagmanager.com/gtag/js?id=GT-X'/>"
            "<link rel='preconnect' href='https://fonts.gstatic.com/'>"
        )
        collected: list[str] = []
        rewriter.rewrite_html_attribute_urls(html, collected_urls=collected)

        # Every captured URL should be a clean URL with no stray quote,
        # whitespace, or attribute fragment embedded in it.
        for url in collected:
            self.assertNotIn("'", url, msg=f"stray quote in {url!r}")
            self.assertNotIn(" ", url, msg=f"stray whitespace in {url!r}")
            self.assertNotIn(">", url, msg=f"stray angle bracket in {url!r}")
            self.assertNotIn("<", url, msg=f"stray angle bracket in {url!r}")

        # And the two real URLs are still captured.
        self.assertTrue(any("googletagmanager.com" in url for url in collected))
        self.assertTrue(any("fonts.gstatic.com" in url for url in collected))

    def test_directory_style_url_resolves_to_directory_index(self) -> None:
        # Regression: a trailing slash used to be sanitized into an empty
        # segment then promoted to "_", producing paths like
        # "./foo/_/index.html" instead of "./foo/index.html".
        rewriter = LocalLinkRewriter()
        self.assertEqual(
            rewriter.normalize_path_for_local("/images/core/emoji/16.0.1/72x72/"),
            "./images/core/emoji/16.0.1/72x72/index.html",
        )

    def test_json_escaped_wayback_wrapped_url_unwraps_to_local_path(self) -> None:
        rewriter = LocalLinkRewriter()
        content = '"u":"https:\\/\\/web.archive.org\\/web\\/20240101000000id_\\/https:\\/\\/voteforit.nz\\/wp-content\\/uploads\\/photo.jpg"'
        rewritten = rewriter.rewrite_json_escaped_urls(content)
        self.assertNotIn("web.archive.org", rewritten)
        self.assertNotIn("voteforit.nz", rewritten)
        self.assertIn("\\/wp-content\\/uploads\\/photo.jpg", rewritten)

    def test_collected_urls_covers_every_syntactic_variant(self) -> None:
        # Single HTML file mixing all four URL forms the rewriter handles.
        # The collected list should hold the canonical https://host/path form
        # for each, regardless of how it appeared in the source.
        rewriter = LocalLinkRewriter()
        content = (
            "<link href='https://voteforit.nz/wp-content/style.css?ver=1' />"
            "<link href='//voteforit.nz/wp-content/protocol-relative.css' />"
            "<img src='https://web.archive.org/web/20240101id_/https://voteforit.nz/wayback-wrapped.png' />"
            "<style>.bg { background: url('https://voteforit.nz/img/bg.png'); }</style>"
            "<script>var cfg = {\"url\":\"https:\\/\\/voteforit.nz\\/api\\/data.json\"};</script>"
        )
        collected: list[str] = []
        # Run every rewrite pass so each kind of URL gets seen.
        rewriter.rewrite_html_attribute_urls(content, collected_urls=collected)
        rewriter.rewrite_css_urls(content, collected_urls=collected)
        rewriter.rewrite_js_urls(content, collected_urls=collected)
        rewriter.rewrite_json_escaped_urls(content, collected_urls=collected)

        # The harvest may yield duplicates because the standard JS pattern
        # also matches quoted HTML attributes — that's fine, the downloader
        # dedups via file_id. We just need every original URL present.
        unique = set(collected)
        self.assertIn("https://voteforit.nz/wp-content/style.css?ver=1", unique)
        self.assertIn("https://voteforit.nz/wp-content/protocol-relative.css", unique)
        self.assertIn("https://voteforit.nz/wayback-wrapped.png", unique)
        self.assertIn("https://voteforit.nz/img/bg.png", unique)
        self.assertIn("https://voteforit.nz/api/data.json", unique)

    def test_collected_urls_default_off_for_backward_compatibility(self) -> None:
        # Calling the methods without ``collected_urls`` should not raise and
        # should not allocate or surface any collection state.
        rewriter = LocalLinkRewriter()
        result = rewriter.rewrite_html_attribute_urls("<a href='https://example.com/'>x</a>")
        self.assertIn("./index.html", result)


class DownloaderTests(unittest.TestCase):
    def test_page_requisites_can_resume_from_existing_html(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = DownloadConfig(target="https://example.com", directory=root, page_requisites=True, max_retries=0)
            downloader = WaybackDownloader(config, transport=FakeTransport({}))

            # Simulate a previous run where the main page already exists locally.
            existing_html = root / "index.html"
            existing_html.parent.mkdir(parents=True, exist_ok=True)
            existing_html.write_text('<html><body><img src="logo.png"></body></html>', encoding="utf-8")
            (root / ".downloaded.txt").write_text("index.html\n", encoding="utf-8")

            downloader._planned_snapshots = Mock(
                return_value=[Snapshot("http://example.com/index.html", 20200101000000, "index.html")]
            )
            downloader._resolve_asset_timestamp = Mock(return_value=20200101000000)
            downloader.archive.download_capture = Mock(
                side_effect=lambda url, timestamp: FetchResult(
                    disposition=FetchDisposition.SAVED,
                    body=b"image-bytes",
                    source_url=url,
                    status_code=200,
                )
            )

            summary = downloader.download()

            self.assertEqual(summary.skipped_existing, 1)
            self.assertTrue((root / "logo.png").exists())
            self.assertEqual((root / "logo.png").read_bytes(), b"image-bytes")
            downloader.archive.download_capture.assert_called_once_with("http://example.com/logo.png", 20200101000000)

    def test_asset_timestamp_lookup_uses_inmemory_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = DownloadConfig(target="https://example.com", directory=Path(temp_dir), page_requisites=True, max_retries=0)
            downloader = WaybackDownloader(config, transport=FakeTransport({}))
            downloader._asset_snapshot_index = downloader._build_asset_index(
                [
                    (20180101000000, "http://example.com/logo.png"),
                    (20200101000000, "http://example.com/logo.png"),
                    (20250101000000, "http://example.com/logo.png"),
                ]
            )
            # Forbid network access for the duration of the lookup so any
            # regression that reintroduces the per-asset CDX call fails loudly.
            downloader.archive.fetch_snapshot_page = Mock(side_effect=AssertionError("must not call CDX"))

            # Picks the newest snapshot at or before the parent timestamp.
            self.assertEqual(
                downloader._resolve_asset_timestamp("https://example.com/logo.png", 20210101000000),
                20200101000000,
            )
            # If every indexed snapshot is newer than the parent we still pick
            # something archived rather than failing.
            self.assertEqual(
                downloader._resolve_asset_timestamp("https://example.com/logo.png", 20100101000000),
                20250101000000,
            )

    def test_asset_timestamp_falls_back_to_parent_when_not_indexed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = DownloadConfig(target="https://example.com", directory=Path(temp_dir), page_requisites=True, max_retries=0)
            downloader = WaybackDownloader(config, transport=FakeTransport({}))
            downloader._asset_snapshot_index = {}
            downloader.archive.fetch_snapshot_page = Mock(side_effect=AssertionError("must not call CDX"))

            self.assertEqual(
                downloader._resolve_asset_timestamp("https://cdn.example.org/jquery.js", 20200101000000),
                20200101000000,
            )

    def test_cross_host_urls_are_skipped_by_default(self) -> None:
        # Defending the crawl-bounding fix: by default, _queue_asset_for_url
        # should ignore URLs from any host other than the target. Without
        # this, the rewriter+page-requisites feedback loop expands into
        # Facebook, X, CDN-hosted scripts, third-party widgets — all of
        # which balloon the queue and produce 404s for assets that aren't
        # archived under the same prefix.
        import queue as queue_mod
        with tempfile.TemporaryDirectory() as temp_dir:
            config = DownloadConfig(target="https://example.com", directory=Path(temp_dir), max_retries=0)
            downloader = WaybackDownloader(config, transport=FakeTransport({}))
            job_queue: queue_mod.Queue = queue_mod.Queue()

            downloader._queue_asset_for_url("https://cdn.example.org/jquery.js", 20240101000000, job_queue)
            downloader._queue_asset_for_url("https://facebook.com/somepage", 20240101000000, job_queue)
            downloader._queue_asset_for_url("https://example.com/local.js", 20240101000000, job_queue)

            self.assertEqual(job_queue.qsize(), 1, "only the same-host URL should be queued")

    def test_cross_host_urls_are_queued_when_flag_is_on(self) -> None:
        import queue as queue_mod
        with tempfile.TemporaryDirectory() as temp_dir:
            config = DownloadConfig(target="https://example.com", directory=Path(temp_dir), cross_host=True, max_retries=0)
            downloader = WaybackDownloader(config, transport=FakeTransport({}))
            job_queue: queue_mod.Queue = queue_mod.Queue()

            downloader._queue_asset_for_url("https://cdn.example.org/jquery.js", 20240101000000, job_queue)
            downloader._queue_asset_for_url("https://example.com/local.js", 20240101000000, job_queue)

            self.assertEqual(job_queue.qsize(), 2, "both hosts should be queued when cross_host=True")

    def test_rewriter_collected_urls_are_queued_for_download(self) -> None:
        # The HTML body contains a JSON-escaped script URL that the page-
        # requisites extractor (HTML attribute scanner) does NOT see. Only the
        # rewriter catches it. The downloader should still queue and download
        # the script via the rewriter-collected URL path.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = DownloadConfig(
                target="https://example.com",
                directory=root,
                page_requisites=True,
                rewrite_to_local=True,
                max_retries=0,
            )
            downloader = WaybackDownloader(config, transport=FakeTransport({}))

            html_body = (
                "<html><body>"
                "<script>var cfg = {\"endpoint\":\"https:\\/\\/example.com\\/api\\/widget.js\"};</script>"
                "</body></html>"
            ).encode("utf-8")
            script_body = b"console.log('widget');"

            downloader._planned_snapshots = Mock(
                return_value=[Snapshot("http://example.com/index.html", 20240101000000, "index.html")]
            )
            downloader._resolve_asset_timestamp = Mock(return_value=20240101000000)

            def fake_download(url: str, timestamp: int):
                if url == "http://example.com/index.html":
                    body = html_body
                elif url == "https://example.com/api/widget.js":
                    body = script_body
                else:
                    raise AssertionError(f"unexpected download {url}")
                return FetchResult(
                    disposition=FetchDisposition.SAVED,
                    body=body,
                    source_url=url,
                    status_code=200,
                )

            downloader.archive.download_capture = Mock(side_effect=fake_download)

            downloader.download()

            # The script discovered only via the rewriter's JSON-escaped pass
            # should now exist on disk where the rewritten reference points.
            widget_path = root / "api" / "widget.js"
            self.assertTrue(widget_path.exists(), f"expected {widget_path} to exist")
            self.assertEqual(widget_path.read_bytes(), script_body)

    def test_existing_files_do_not_trigger_rate_limit_sleep(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = DownloadConfig(target="https://example.com", directory=root, rate_limit=0.25, max_retries=0)
            downloader = WaybackDownloader(config, transport=FakeTransport({}))

            existing_html = root / "index.html"
            existing_html.parent.mkdir(parents=True, exist_ok=True)
            existing_html.write_text("already here", encoding="utf-8")

            downloader._planned_snapshots = Mock(
                return_value=[Snapshot("http://example.com/index.html", 20200101000000, "index.html")]
            )

            with patch("wayback_downloader.downloader.time.sleep") as mocked_sleep:
                downloader.download()

            mocked_sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
