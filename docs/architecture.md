# Architecture Guide

This document explains how the downloader is organized internally and how a
request moves through the system.

## Design Goals

The Python rewrite favors:

- small modules with single responsibilities
- typed configuration and data models
- deterministic filename mapping
- resumable state handling
- offline-testable HTTP behavior

## High-Level Flow

For a normal download run, the control flow is:

1. Parse CLI arguments into a `DownloadConfig`.
2. Build a `WaybackDownloader`.
3. Load or fetch the CDX snapshot listing.
4. Convert raw CDX records into planned `Snapshot` objects.
5. Skip already-downloaded file IDs using `.downloaded.txt`.
6. Download remaining snapshots with worker threads.
7. Optionally rewrite saved files for local browsing.
8. Optionally queue page requisites.
9. Optionally recurse into subdomains.
10. Clean up or preserve state files.

## Module Map

### `wayback_downloader.cli`

- Defines the command-line interface.
- Translates parsed arguments into a `DownloadConfig`.
- Dispatches to one of three top-level actions:
  - list files
  - rewrite local files only
  - full download

### `wayback_downloader.config`

- Defines `DownloadConfig`, the typed runtime configuration object.
- Normalizes the output directory and validates numeric inputs.
- Computes the backup name used by the default output path.

### `wayback_downloader.archive`

- Implements `ArchiveClient`.
- Knows how to:
  - normalize targets into CDX query forms
  - fetch CDX pages
  - fetch archived response bodies
  - follow redirects
  - tolerate incorrect `Content-Encoding` values

This module contains the logic that maps user intent to Wayback/CDX API
requests.

### `wayback_downloader.transport`

- Provides the transport abstraction used by `ArchiveClient`.
- The default implementation, `HTTPArchiveTransport`, manages reusable HTTP
  connections.
- The abstraction makes tests easy because the suite can inject a fake
  transport instead of touching the network.

### `wayback_downloader.models`

- Defines the small data objects used by the application:
  - `Snapshot`
  - `HTTPResponse`
  - `FetchResult`
  - `DownloadSummary`
  - related enums/data containers

### `wayback_downloader.snapshots`

- Converts raw CDX tuples into planned `Snapshot` objects.
- Implements the three selection modes:
  - latest per logical file
  - all timestamps
  - composite snapshot at a given time

### `wayback_downloader.filters`

- Applies include/exclude URL filtering.
- Supports literal substring matching and regex literals written as
  `/pattern/flags`.

### `wayback_downloader.paths`

- Centralizes logical file ID and filesystem path mapping.
- Handles:
  - percent-decoding
  - best-effort byte repair
  - query-string hashing into `__q...` filenames
  - Windows-invalid character escaping
  - restructuring a blocking file into `index.html` when a directory is later
    required

This module is critical because download identity, resume state, and local link
rewriting all depend on the same mapping rules.

### `wayback_downloader.state`

- Reads and writes `.cdx.json` and `.downloaded.txt`.
- Treats stale `.downloaded.txt` entries as invalid if the corresponding file
  is missing on disk.

### `wayback_downloader.url_rewrite`

- Rewrites saved content for local browsing.
- Handles:
  - archived Wayback URLs
  - direct absolute URLs
  - HTML attributes
  - CSS `url(...)`
  - JavaScript string literals
  - subdomain-to-local rewrites

### `wayback_downloader.requisites`

- Extracts linked asset references from saved page content.
- Used by `--page-requisites`.

### `wayback_downloader.subdomains`

- Finds subdomain references in downloaded files.
- Used by `--recursive-subdomains`.

### `wayback_downloader.downloader`

- Orchestrates everything.
- Builds worker threads.
- Applies resume logic.
- Queues extra jobs for page requisites and subdomains.
- Produces the final `DownloadSummary`.

## Core Data Structures

### `DownloadConfig`

The full user-selected behavior of a run. This includes:

- target selection
- output directory
- timestamp boundaries
- filtering
- concurrency
- retry/timeouts
- local rewrite behavior
- subdomain/page-requisite behavior

### `Snapshot`

A planned download unit with three fields:

- `original_url`
- `timestamp`
- `file_id`

`file_id` is the logical local identity used by both resume tracking and
filesystem path mapping.

### State Files

Two files drive resumability:

- `.cdx.json`
  Cached CDX results so a rerun does not have to re-fetch the entire snapshot
  listing.
- `.downloaded.txt`
  One logical file ID per successfully written capture.

## Snapshot Planning Details

The CDX API returns raw `(timestamp, original)` tuples. The planner turns those
into `Snapshot` objects after filtering and file-ID sanitation.

### Latest Mode

- One logical file ID survives.
- The newest timestamp wins.

### All-Timestamps Mode

- The timestamp is prefixed into the logical file ID.
- This prevents collisions between captures of the same URL at different times.

### Composite Mode

- Only captures at or before `snapshot_at` are eligible.
- For each logical file ID, the newest eligible version wins.

## Filename Mapping Rules

The downloader makes archived URLs filesystem-safe without losing too much
identity.

Important rules:

- query strings are hashed into the filename
- percent-encoded bytes are repeatedly decoded
- Windows-invalid characters are escaped as `%XX`
- directory-like paths become `index.html`

Example:

```text
/assets/app.css?version=123
```

becomes something like:

```text
assets/app__q12ab34cd56ef.css
```

## Worker Model

The downloader uses a simple queue-based worker pool.

- A fixed number of threads consume `Snapshot` jobs.
- A job may queue additional jobs when page requisites are enabled.
- Existing local files short-circuit quickly and do not incur rate-limit sleep.
- Real network fetches sleep for `rate_limit` seconds between jobs per worker.

## Page Requisites

When `--page-requisites` is enabled:

- downloaded HTML-like pages are scanned for linked assets
- relative references are resolved against the page URL
- HTML pages are not recursively queued as requisites
- the downloader asks the CDX API for the best matching asset timestamp at or
  before the parent page timestamp

Already-downloaded HTML pages are also eligible for page-requisite discovery on
later runs, which makes the feature useful as a resume or second-pass workflow.

## Subdomain Mirroring

When `--recursive-subdomains` is enabled:

- the downloader scans saved content for URLs that point to subdomains of the
  base domain
- each subdomain is mirrored into `subdomains/<host>/`
- optional local rewriting can then rewrite those URLs to the local subdomain
  mirror

## Failure Handling

The downloader is designed to fail softly when possible:

- corrupt `.cdx.json` files are ignored and replaced
- stale `.downloaded.txt` entries are ignored if the file is missing
- bad `gzip`/`deflate` headers fall back to raw bytes
- state files are kept after failed runs unless `--reset` is in effect

## Testing Strategy

Most behavior is tested without live network access by injecting a fake
transport into `ArchiveClient` and using temporary directories for output.

That keeps the tests deterministic and focused on downloader behavior rather
than archive availability.
