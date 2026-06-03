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

### Rewrite passes are gated by file type
`rewrite_file` only runs CSS passes on `.css` and HTML-like files, JS passes on `.js` and HTML-like files, srcset/HTML-attribute passes on HTML-like files only. HTML-like files (`.html`, `.htm`, `.php`, `.asp`, `.aspx`, `.jsp`) get every pass because they can embed any other syntax inline (`<style>`, `<script>`, attributes). The "every pass runs on every file" model previously caused real corruption: the case-insensitive CSS `url\(` matched `URL(` inside identifiers like `compareURL(` in minified JS, and the `?` from optional chaining (`obj?.prop`) looked like a query string. Result: silent JS corruption. File-gating prevents recurrences without needing every regex to be defensively bulletproof.

### CSS `url()` substitutions must preserve the original quote
Hardcoding `f'url("...")'` (double quotes) silently corrupts HTML `style="..."` attributes: the injected `"` closes the outer attribute early and the rest of the URL parses as more attributes. The CSS substitution patterns capture the source's opening and closing quote (or its absence) and reuse them in the substitution. Tests `test_css_url_quote_style_*` lock all three cases (none, single, double).

### CSS relative-query pass excludes JS optional chaining
`_CSS_RELATIVE_QUERY` requires the query to start with `[a-zA-Z0-9_]`. That excludes `?.`, `?[`, `?(` (JS optional chaining forms). Without this, the pattern matched `compareURL(SR7.M[e].imgList[t]?.old,i)` as a "CSS url() with relative path + query" and folded the JS expression into a `__q<hash>` filename. The corruption was unrecoverable — once `imgList[t]?.old` became `imgList[t]__q<hash>.old`, the optional chaining was gone.

### JS string URLs use *base-URL* semantics, not document semantics
`rewrite_js_urls` and `rewrite_json_escaped_urls` pass `as_base_url=True` to `normalize_path_for_local`. That preserves a trailing slash and skips the `/index.html` appendage, because JS strings are usually used as bases for concatenation (`base + "subpath"`). Without this, WordPress's `endpoint = "/wp-json/"` becomes `"./wp-json/index.html"` and `endpoint + "wp/v2/users/me"` produces `"./wp-json/index.htmlwp/v2/users/me"`. HTML attribute rewrites still get the `/index.html` treatment (browsers navigate to those URLs as documents).

### Relative CSS `url()` refs with queries need their own pass
The downloader folds `?ver=4.2` into `__q<hash>` filename suffixes. The absolute-URL CSS pass handles `url("https://host/foo.woff?v=4.2")` via `sanitize_reference_path`, but bare relative refs (`url("fonts/foo.woff?v=4.2")`) skip the absolute pass entirely. `rewrite_css_relative_query_urls` is a small targeted pass that folds the query into the filename for relative refs only — guarded by a negative lookahead for `data:`/`https?:`/`//` so it doesn't double-fold absolute URLs.

### `srcset` values need a dedicated rewrite pass
WordPress's responsive images emit `srcset="url1 2000w, url2 1024w, url3 300w"`. The standard JS URL pattern matches any quoted absolute-URL-looking string, so it'd capture the whole comma-joined value as one URL. Likewise, `PageRequisitesExtractor`'s old heuristic only checked for the literal strings ` 1x` and ` 2w` — missing every WordPress width descriptor. Fix: `LocalLinkRewriter.rewrite_srcset_urls` runs **first**, splits on commas, rewrites each URL with its descriptor preserved. `PageRequisitesExtractor._split_srcset` now accepts any `\d+(w|x)` descriptor.

### Crawl is bounded to the target host by default
After the rewriter started collecting URLs as a discovery source, the crawl exploded into Facebook, X, Vimeo, CDN jQuery, Wellington council, wordpress.org, etc. `_queue_asset_for_url` now skips any URL whose host doesn't match the target. The `--cross-host` CLI flag opts back in for cases where you genuinely want cross-origin assets mirrored. Same-host is the right default because cross-host URLs don't map cleanly into the local namespace (see the cross-host bug above) and bleed the queue into navigation links.

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
