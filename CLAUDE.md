# wayback-downloader-python

Python port of the Ruby `wayback-machine-downloader` tool. Mirrors Wayback Machine captures of a site to a local directory tree, with optional inline link rewriting so the saved tree browses offline.

## Workflows

Three CLI modes, switched by flags:

| Command | What it does |
| --- | --- |
| `python -m wayback_downloader <url>` | Mirror: CDX listing → download every capture under the site. |
| `python -m wayback_downloader <url> --list` | Print JSON of planned captures without downloading. |
| `python -m wayback_downloader <dir> --local-only` | Re-rewrite an existing tree to use relative paths. No network. |

Common add-ons:

- `--page-requisites` — also fetch HTML-linked assets (CSS/JS/images).
- `--local` — rewrite URLs to relative paths as files land (so each saved file is browsable immediately).
- `-c 5` — concurrent workers; default is 1 and big sites with `--page-requisites` benefit enormously from 5–10.
- `--reset` — discard the `.cdx.json` / `.downloaded.txt` state for a fresh run.

Output defaults to `./websites/<sanitized-host>/`.

## Architecture (one line per module)

```
cli.py            argparse → DownloadConfig → WaybackDownloader
config.py         DownloadConfig dataclass + output-path derivation
models.py         Snapshot, HTTPResponse, FetchResult, DownloadSummary
transport.py      ArchiveTransport protocol + per-thread http.client pool
archive.py        ArchiveClient: CDX API + /web/{ts}id_/ capture endpoint
snapshots.py      SnapshotPlanner: latest / all-timestamps / composite-at-ts
downloader.py     WaybackDownloader: orchestrates CDX → queue → fetch → rewrite → page-requisites
requisites.py     PageRequisitesExtractor: regex over HTML href/src/url()/srcset
url_rewrite.py    LocalLinkRewriter: absolute → relative; also collects URLs it touches
paths.py          LocalPathMapper + OutputLayout; URL → filesystem path (Windows-aware)
text.py           Encoding helpers; repeated_percent_decode
state.py          .cdx.json (CDX cache) + .downloaded.txt (resume DB) under backup root
subdomains.py     Recursive subdomain discovery + dispatch
filters.py        URLFilter for --only/--exclude (supports Ruby /.../i regex literals)
```

Two cross-cutting flows worth understanding:

1. **Page-requisites discovery has two sources that funnel into one queue.** The `PageRequisitesExtractor` scans saved HTML attributes; the `LocalLinkRewriter` (when called with `collected_urls=[]`) also reports every absolute URL it rewrites — including JS string literals and JSON-escaped script-block URLs that the extractor misses. Both call `WaybackDownloader._queue_asset_for_url` for the same dedup + file_id + timestamp logic.
2. **Asset timestamp resolution is in-memory.** `_resolve_asset_timestamp` looks up the URL in an index built from the initial site-wide CDX listing. It does **not** call CDX per asset — that earlier behavior swamped the API and is now guarded by a `Mock(side_effect=AssertionError("must not call CDX"))` trap in the tests.

## Conventions

- **Tests are the spec.** Every behavior change comes with a regression test in `tests/test_wayback_downloader.py`. Run `python -m unittest tests.test_wayback_downloader` — there are 26 tests at time of writing, all should be green.
- **Comments explain *why*, never *what*.** Most code is left bare; identifier names carry the meaning. A comment exists only when removing it would confuse a future reader (e.g., the `repeated_percent_decode` docstring explains the latin-1 round-trip bug because nobody would intuit it from the loop body).
- **No backwards-compat hacks.** Renames are real renames; removed code is deleted, not `// removed` commented out. The `wayback_downloader` API isn't public — callers are tests + CLI.
- **Threaded code stays simple.** Worker threads in `WaybackDownloader` share state through a `queue.Queue` and a single `Lock`; per-worker accumulators (see `LocalLinkRewriter.rewrite_tree`'s threaded path) are merged at the end rather than mutated concurrently.
- **Windows-aware paths.** `paths.py:_filesystem_safe_segment` substitutes `:*?"<>|&=\\/` to percent-encoded equivalents on Windows. `LocalPathMapper.ensure_directory` handles the classic "we saved `/foo` as a file, now we need `/foo/bar`" collision by moving the blocking file to `foo/index.html` and retrying.

## Gotchas (read these before touching things)

### `repeated_percent_decode` had a memory runaway
The stop condition tested for a fixed point via `bytes.decode("latin-1")` round-trip. For any input containing non-ASCII characters the round-trip *grew* the working string each iteration (`café` → `cafÃ©` → `cafÃ\x83Â©` → …) and eventually OOMed. Fix: stop when no `%` escapes remain, cap at 8 iterations, decode via UTF-8 with raw-bytes fallback. See `text.py` + `RepeatedPercentDecodeTests`.

### CDX returns HTTP 400 to mean "no more pages"
`archive.py:fetch_snapshot_page` downgrades 400-on-page-N (N > 0) to DEBUG. A 400 on page 0 is a real error and stays as WARNING. Don't add a generic retry for 4xx — the Ruby tool had the same trap.

### `_resolve_asset_timestamp` MUST NOT call CDX per asset
Earlier behavior issued one CDX search per linked asset, which took ~132s per asset under timeout+retry conditions and turned a 15-minute download into a ~6-hour one. The fix builds an in-memory index from the initial site-wide CDX listing; cross-origin assets fall back to `parent_timestamp` and let Wayback's `id_` URL auto-redirect to the closest snapshot. The tests use a `Mock(side_effect=AssertionError("must not call CDX"))` trap — if you reintroduce per-asset CDX calls, you'll see why.

### The URL rewriter has four syntactic variants to handle
Real-world HTML/CSS/JS embeds URLs in at least four forms:

1. Absolute (`https://host/path`)
2. Protocol-relative (`//host/path` — WordPress uses this)
3. JSON-escaped inside `<script>` blocks (`https:\/\/host\/path` — WordPress `wpemojiSettings`-style configs)
4. Wayback-wrapped (`https://web.archive.org/web/{ts}id_/https://host/path`)

Each form has both a "standard" and a "Wayback-wrapped" variant. The rewriter handles all eight in `rewrite_html_attribute_urls` / `rewrite_css_urls` / `rewrite_js_urls` / `rewrite_json_escaped_urls`. If you add a new URL location (e.g., `<style>` inline blocks with a non-`url()` syntax), the rewriter needs an explicit pass — there's no generic "find URLs anywhere" fallback.

### Cross-host URL rewriting is a known pre-existing bug
The rewriter strips the host from cross-host URLs (`https://cdn.example.com/foo.png` → `./foo.png` relative to the file). But the downloader saves cross-host assets at `https%3A/cdn.example.com/foo.png` (full URL sanitized into a path). The rewritten reference doesn't match where the file lands → broken local link. Fixing this requires either: (a) the rewriter preserving the host in the relative path, or (b) the downloader saving cross-host assets at the host-stripped location. Both have trade-offs; not yet addressed.

### Trailing slashes used to produce `/_/index.html`
`sanitize_reference_path` split paths on `/` and promoted empty-segment-after-trailing-slash to `"_"` via the all-invalid-chars fallback rule. So `/foo/` rewrote to `./foo/_/index.html`. Fixed by filtering empty segments *before* the fallback runs. The `or "_"` fallback now only fires for genuinely-sanitized-to-empty segments (e.g., `"<<<"` → `""` → `"_"`).

## Running and debugging

```sh
# Full test suite
python -m unittest tests.test_wayback_downloader

# Verbose with names
python -m unittest tests.test_wayback_downloader -v

# Filter
python -m unittest tests.test_wayback_downloader -k percent_decode

# Quick download of one captured page
python -m wayback_downloader https://example.com/specific/page --exact-url

# Mirror a small site with assets, fast
python -m wayback_downloader https://example.com --page-requisites --local -c 5
```

State files live under the backup directory:
- `.cdx.json` — cached CDX listing; deleted by `--reset`.
- `.downloaded.txt` — newline-separated file_ids successfully written; powers resume.

Both are deleted on successful runs unless `--keep` is passed or the run had failures.
