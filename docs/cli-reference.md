# CLI Reference

This document describes the command-line interface exposed by
`wayback-machine-downloader` and `python -m wayback_downloader`.

## Invocation

Installed script:

```bash
wayback-machine-downloader [options] target
```

Module form:

```bash
python -m wayback_downloader [options] target
```

## Target Semantics

The positional `target` is usually one of:

- a full site URL, such as `https://example.com`
- a host name, such as `example.com`
- a directory URL, such as `https://example.com/wiki/`
- a single exact file URL when combined with `--exact-url`
- a local directory path when combined with `--local-only`

By default, host-only and trailing-slash directory targets are normalized into
prefix-style CDX queries so the downloader asks for more than just the index
page capture.

## Core Commands

Download the latest capture of each logical file:

```bash
python -m wayback_downloader https://example.com
```

List captures without downloading:

```bash
python -m wayback_downloader --list https://example.com
```

Rewrite an existing download tree locally:

```bash
python -m wayback_downloader --local-only ./websites/example.com
```

## Options

### Output

`-d, --directory PATH`

- Write files into `PATH` instead of `./websites/<backup-name>`.
- The directory is resolved to an absolute path.

### Snapshot Selection

`-s, --all-timestamps`

- Keep every capture instead of only the latest logical file version.

`--snapshot-at TIMESTAMP`

- Build a best-effort snapshot as of a point in time.
- For each logical file ID, the newest capture at or before `TIMESTAMP` wins.

`-f, --from TIMESTAMP`

- Ignore captures older than `TIMESTAMP`.

`-t, --to TIMESTAMP`

- Ignore captures newer than `TIMESTAMP`.

### Target Matching

`-e, --exact-url`

- Treat the target as one exact resource instead of a site/directory prefix.
- Disables the automatic `/*` normalization behavior.

`-o, --only FILTER`

- Include only matching URLs.
- Accepts either a literal substring or a regex written as `/pattern/flags`.

`-x, --exclude FILTER`

- Exclude matching URLs.
- Accepts either a literal substring or a regex written as `/pattern/flags`.

Examples:

```bash
python -m wayback_downloader --only "/\\.(png|jpg)$/i" https://example.com
python -m wayback_downloader --exclude admin https://example.com
```

### HTTP and Response Handling

`-a, --all`

- Include 30x, 40x, and 50x captures instead of focusing only on successful
  responses.
- Useful when you want redirect/error pages themselves rather than just their
  final successful targets.

`-r, --rewritten`

- Download rewritten Wayback-hosted versions rather than raw `id_` captures.

`--rt, --retry N`

- Maximum number of retries for transient failures during CDX or capture
  retrieval.

`--timeout SECONDS`

- Socket timeout used by the HTTP transport.

### CDX Query Behavior

`--keep-duplicates`

- Do not request `collapse=digest` from the CDX API.
- Useful when identical response bodies should still be preserved as distinct
  captures.

`-p, --maximum-snapshot N`

- Maximum CDX pages to scan.

### Concurrency

`-c, --concurrency N`

- Worker count for download processing.
- Also influences concurrent CDX page fetching.

### Local Rewriting

`--local`

- Rewrite downloaded HTML/CSS/JS/server-side pages so absolute URLs become
  local relative references after download.

`--local-only`

- Only run the rewrite phase on an existing local directory.
- In this mode the positional `target` is interpreted as a filesystem path.

### Resume and State Files

`--reset`

- Delete `.cdx.json` and `.downloaded.txt` before the run.

`--keep`

- Keep `.cdx.json` and `.downloaded.txt` after a successful run.

Default behavior:

- state files are removed after a successful run
- state files are kept when the run finishes with failures, so it can resume

### Asset and Subdomain Discovery

`--page-requisites`

- After an HTML-like page is saved, scan it for CSS, JS, image, and similar
  asset references and queue them for download.

`--recursive-subdomains`

- Scan downloaded content for subdomains of the base domain and mirror them
  into `subdomains/<host>/`.

`--subdomain-depth N`

- Maximum number of recursive discovery rounds for subdomains.

### Utility Flags

`-l, --list`

- Print the planned capture list as JSON and exit.

`--version`

- Print the package version and exit.

## Exit Behavior

The CLI returns a zero exit code on successful execution. Invalid CLI usage,
such as omitting a target or using `--local-only` with a missing directory,
raises a `SystemExit` with an explanatory message.

## Practical Recipes

Download a site as of late 2012 and rewrite links locally:

```bash
python -m wayback_downloader --to 20121231 --local https://example.com
```

Fetch all captures but keep state files for incremental inspection:

```bash
python -m wayback_downloader --all-timestamps --keep https://example.com
```

Mirror only CSS and JavaScript:

```bash
python -m wayback_downloader --only "/\\.(css|js)$/i" https://example.com
```

Use page requisites to improve page completeness:

```bash
python -m wayback_downloader --page-requisites --local https://example.com
```
