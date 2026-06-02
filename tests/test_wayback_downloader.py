from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from wayback_downloader.archive import ArchiveClient
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
