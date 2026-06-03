from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import __version__
from .config import DownloadConfig
from .downloader import WaybackDownloader


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI surface to match the Ruby downloader's options closely."""

    parser = argparse.ArgumentParser(description="Download websites from the Internet Archive Wayback Machine.")
    parser.add_argument("target", nargs="?", help="Website URL, host, or a directory when using --local-only.")
    parser.add_argument("-d", "--directory", type=Path, help="Directory to save downloaded files into.")
    parser.add_argument("-s", "--all-timestamps", action="store_true", help="Download all snapshots for each file.")
    parser.add_argument("-f", "--from", dest="from_timestamp", type=int, help="Only include captures on or after this timestamp.")
    parser.add_argument("-t", "--to", dest="to_timestamp", type=int, help="Only include captures on or before this timestamp.")
    parser.add_argument("-e", "--exact-url", action="store_true", help="Download only the exact target URL instead of the full site.")
    parser.add_argument("-o", "--only", dest="only_filter", help="Restrict downloads to URLs matching this filter.")
    parser.add_argument("-x", "--exclude", dest="exclude_filter", help="Skip URLs matching this filter.")
    parser.add_argument("-a", "--all", dest="include_all_responses", action="store_true", help="Include 30x, 40x, and 50x captures.")
    parser.add_argument("--keep-duplicates", action="store_true", help="Disable digest collapsing in CDX results.")
    parser.add_argument("-c", "--concurrency", type=int, default=1, help="Number of concurrent download workers.")
    parser.add_argument("-p", "--maximum-snapshot", dest="maximum_pages", type=int, default=100, help="Maximum CDX pages to query.")
    parser.add_argument("-l", "--list", dest="list_only", action="store_true", help="List files as JSON without downloading.")
    parser.add_argument("-r", "--rewritten", action="store_true", help="Download rewritten Wayback files instead of raw originals.")
    parser.add_argument("--local", dest="rewrite_to_local", action="store_true", help="Rewrite downloaded files for local browsing.")
    parser.add_argument("--local-only", action="store_true", help="Only rewrite an existing download directory.")
    parser.add_argument("--reset", action="store_true", help="Delete state files and start again.")
    parser.add_argument("--keep", dest="keep_state", action="store_true", help="Keep state files after a successful run.")
    parser.add_argument("--rt", "--retry", dest="max_retries", type=int, default=3, help="Maximum retry attempts for failed requests.")
    parser.add_argument("--snapshot-at", type=int, help="Build a composite snapshot at this timestamp.")
    parser.add_argument("--recursive-subdomains", action="store_true", help="Discover and download linked subdomains.")
    parser.add_argument("--subdomain-depth", type=int, default=1, help="Maximum recursion depth for subdomain discovery.")
    parser.add_argument("--page-requisites", action="store_true", help="Queue linked page assets after downloading HTML files.")
    parser.add_argument(
        "--cross-host",
        action="store_true",
        help="Also queue and download URLs from hosts other than the target. Off by default — without it, only same-host URLs are mirrored, which keeps the crawl bounded.",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def build_config(args: argparse.Namespace) -> DownloadConfig:
    """Translate parsed CLI arguments into the typed runtime config."""

    if args.local_only:
        if not args.target:
            raise SystemExit("A directory is required when using --local-only.")
        directory = Path(args.target).expanduser().resolve()
        if not directory.exists():
            raise SystemExit(f"Directory does not exist: {directory}")
        return DownloadConfig(
            target=args.target,
            directory=directory,
            all_timestamps=args.all_timestamps,
            from_timestamp=args.from_timestamp,
            to_timestamp=args.to_timestamp,
            exact_url=args.exact_url,
            only_filter=args.only_filter,
            exclude_filter=args.exclude_filter,
            include_all_responses=args.include_all_responses,
            keep_duplicates=args.keep_duplicates,
            maximum_pages=args.maximum_pages,
            concurrency=args.concurrency,
            list_only=args.list_only,
            rewritten=args.rewritten,
            rewrite_to_local=args.rewrite_to_local,
            local_only=True,
            reset=args.reset,
            keep_state=args.keep_state,
            max_retries=args.max_retries,
            snapshot_at=args.snapshot_at,
            recursive_subdomains=args.recursive_subdomains,
            subdomain_depth=args.subdomain_depth,
            page_requisites=args.page_requisites,
            cross_host=args.cross_host,
            timeout=args.timeout,
        )

    if not args.target:
        raise SystemExit("A website URL or host is required.")

    return DownloadConfig(
        target=args.target,
        directory=args.directory,
        all_timestamps=args.all_timestamps,
        from_timestamp=args.from_timestamp,
        to_timestamp=args.to_timestamp,
        exact_url=args.exact_url,
        only_filter=args.only_filter,
        exclude_filter=args.exclude_filter,
        include_all_responses=args.include_all_responses,
        keep_duplicates=args.keep_duplicates,
        maximum_pages=args.maximum_pages,
        concurrency=args.concurrency,
        list_only=args.list_only,
        rewritten=args.rewritten,
        rewrite_to_local=args.rewrite_to_local,
        local_only=False,
        reset=args.reset,
        keep_state=args.keep_state,
        max_retries=args.max_retries,
        snapshot_at=args.snapshot_at,
        recursive_subdomains=args.recursive_subdomains,
        subdomain_depth=args.subdomain_depth,
        page_requisites=args.page_requisites,
        timeout=args.timeout,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point used by both ``python -m`` and the console script."""

    parser = build_parser()
    args = parser.parse_args(argv)
    config = build_config(args)
    downloader = WaybackDownloader(config)

    if config.local_only:
        rewritten_count = downloader.rewrite_local_files()
        print(f"Rewrote {rewritten_count} files in {config.output_path}")
        return 0

    if config.list_only:
        print(json.dumps(downloader.list_files(), indent=2))
        return 0

    downloader.download()
    return 0
