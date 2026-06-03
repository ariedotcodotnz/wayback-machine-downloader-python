# Wayback Machine Downloader

[![CI](https://github.com/ariedotcodotnz/wayback-machine-downloader-python/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/ariedotcodotnz/wayback-machine-downloader-python/actions/workflows/ci.yml)
[![Publish to TestPyPI](https://github.com/ariedotcodotnz/wayback-machine-downloader-python/actions/workflows/testpypi.yml/badge.svg)](https://github.com/ariedotcodotnz/wayback-machine-downloader-python/actions/workflows/testpypi.yml)
[![Publish to PyPI](https://github.com/ariedotcodotnz/wayback-machine-downloader-python/actions/workflows/release.yml/badge.svg)](https://github.com/ariedotcodotnz/wayback-machine-downloader-python/actions/workflows/release.yml)

A Python port of the original Ruby [`wayback-machine-downloader`](https://github.com/StrawberryMaster/wayback-machine-downloader), built for users who prefer a Python-based workflow for downloading archived websites from the Internet Archive Wayback Machine.

This tool helps recover, mirror, and archive old websites from Wayback Machine snapshots. It is useful for digital preservation, website recovery, static site restoration, OSINT research, historical web analysis, and rebuilding sites that are no longer online.

This Python version includes a number of extra fixes, improvements, and quality-of-life changes over the original Ruby implementation.

## Highlights

- Download the latest capture of each file for a target
- Download every timestamped capture with timestamp-prefixed file IDs
- Build a composite snapshot as of a point in time
- Resume interrupted runs using `.cdx.json` and `.downloaded.txt`
- Rewrite archived links for local browsing with `--local`
- Discover linked page assets with `--page-requisites`
- Recursively mirror subdomains with `--recursive-subdomains`
- Keep the implementation dependency-light and fully testable offline

## Requirements

- Python 3.10 or newer

## Installation

Install the published package from PyPI:

```bash
python -m pip install wayback-machine-downloader
```

The PyPI distribution name is `wayback-machine-downloader`; the import package
remains `wayback_downloader`.

Install the package in editable mode while developing:

```bash
python -m pip install -e .
```

Or run it directly from the repository:

```bash
python -m wayback_downloader --help
```

The package also exposes a console script after installation:

```bash
wayback-machine-downloader --help
```

## Quick Start

Download the latest version of every file for a site:

```bash
python -m wayback_downloader https://example.com
```

List the planned captures without downloading:

```bash
python -m wayback_downloader --list https://example.com
```

Download all historical captures:

```bash
python -m wayback_downloader --all-timestamps https://example.com
```

Build a composite snapshot as of a specific timestamp:

```bash
python -m wayback_downloader --snapshot-at 20130101000000 https://example.com
```

Rewrite an existing downloaded tree for local browsing:

```bash
python -m wayback_downloader --local-only ./websites/example.com
```

## Output Layout

By default, downloads are written under:

```text
./websites/<backup-name>/
```

`<backup-name>` is usually the target host. For example:

```text
websites/example.com/
```

The downloader also uses two state files in the output directory:

- `.cdx.json`
  Cached snapshot listing fetched from the CDX API.
- `.downloaded.txt`
  Logical file IDs that have been written successfully.

These files let later runs resume instead of starting from scratch. Use
`--reset` to delete them before a run, or `--keep` to preserve them after a
successful run.

## Common Workflows

Download only one exact URL:

```bash
python -m wayback_downloader --exact-url https://example.com/index.html
```

Limit by timestamp range:

```bash
python -m wayback_downloader --from 20060101 --to 20071231 https://example.com
```

Filter URLs:

```bash
python -m wayback_downloader --only "/\\.(css|js|png)$/i" https://example.com
python -m wayback_downloader --exclude admin https://example.com
```

Download pages and immediately queue linked assets:

```bash
python -m wayback_downloader --page-requisites --local https://example.com
```

Recursively mirror discovered subdomains:

```bash
python -m wayback_downloader --recursive-subdomains --subdomain-depth 2 https://example.com
```

## Snapshot Selection Modes

The downloader supports three selection strategies:

1. Latest per logical file
   Default behavior. For each logical file ID, the newest capture wins.
2. All timestamps
   Enabled with `--all-timestamps`. The timestamp becomes part of the logical
   file ID so every capture is kept.
3. Composite snapshot
   Enabled with `--snapshot-at`. For each file, choose the newest capture at
   or before the requested timestamp.

## URL and Filename Behavior

Several implementation details are worth knowing because they influence the
output tree:

- Host and trailing-slash directory targets are normalized into CDX prefix
  queries unless `--exact-url` is used.
- Query strings are folded into filenames using a short digest, such as
  `app__q12ab34cd56ef.css`.
- Directory-like captures are stored as `.../index.html`.
- If a file blocks a needed directory later in the run, it is moved to
  `index.html` so both captures can coexist.

## Local Rewriting

The `--local` option rewrites archived absolute URLs into local relative
references after files are saved. It handles:

- Wayback-hosted rewritten URLs
- direct absolute HTTP/HTTPS links
- HTML attributes such as `href`, `src`, and `action`
- CSS `url(...)` references
- JavaScript string literals containing absolute URLs

`--local-only` performs only the rewrite phase on an existing directory and
does not contact the archive.

## Documentation

- [CLI reference](https://github.com/ariedotcodotnz/wayback-machine-downloader-python/blob/master/docs/cli-reference.md)
- [Architecture guide](https://github.com/ariedotcodotnz/wayback-machine-downloader-python/blob/master/docs/architecture.md)
- [Development and testing](https://github.com/ariedotcodotnz/wayback-machine-downloader-python/blob/master/docs/development.md)

## Publishing

### GitHub Actions automation

This repository includes GitHub Actions workflows for:

- CI testing on push and pull request across Python 3.10-3.13, plus a Windows smoke job
- building `sdist` and `wheel` artifacts on every CI run
- manual publishing to TestPyPI
- publishing to PyPI when a GitHub Release is published

To use Trusted Publishing with the included workflows:

1. Create GitHub environments named `testpypi` and `pypi`.
2. In TestPyPI, register a trusted publisher for this repository using workflow file `testpypi.yml` and environment `testpypi`.
3. In PyPI, register a trusted publisher for this repository using workflow file `release.yml` and environment `pypi`.

After that:

- run the `Publish to TestPyPI` workflow manually from the Actions tab to test uploads
- publish a GitHub Release to trigger the `Publish to PyPI` workflow

### Manual publishing

Build and validate the distribution archives:

```bash
python -m pip install --upgrade build twine
python -m build
python -m twine check dist/*
```

Upload to TestPyPI first:

```bash
python -m twine upload --repository testpypi dist/*
```

Upload to PyPI:

```bash
python -m twine upload dist/*
```

Use an API token when Twine prompts for credentials:

- username: `__token__`
- password: your `pypi-...` token

## Testing

Run the test suite:

```bash
python -B -m unittest discover -s tests -t .
```

Compile modules as a quick import sanity check:

```bash
python -m compileall wayback_downloader tests
```

The tests use fake transports and temporary directories, so they do not depend
on live access to `web.archive.org`.
