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
