# Development and Testing

This guide is for contributors working on the Python rewrite.

## Repository Layout

```text
wayback_downloader/
  archive.py
  cli.py
  config.py
  downloader.py
  filters.py
  models.py
  paths.py
  requisites.py
  snapshots.py
  state.py
  subdomains.py
  text.py
  transport.py
  url_rewrite.py

tests/
  test_wayback_downloader.py
```

## Local Setup

Create an editable install:

```bash
python -m pip install -e .
```

This project currently has no third-party runtime dependencies.

## Running the CLI During Development

Use the module form:

```bash
python -m wayback_downloader --help
```

Or, after editable install:

```bash
wayback-machine-downloader --help
```

## Running Tests

Primary test command:

```bash
python -B -m unittest discover -s tests -t .
```

The `-B` flag prevents Python from writing fresh `.pyc` files during the test
run. That keeps the working tree cleaner, especially now that `__pycache__`
directories are ignored.

Optional import/compile sanity check:

```bash
python -m compileall wayback_downloader tests
```

## Test Philosophy

The suite intentionally avoids live archive traffic.

Instead, it relies on:

- `FakeTransport` objects that return canned `HTTPResponse` values
- temporary directories for downloader output
- direct assertions on file layout, state files, and rewritten content

This means tests are:

- deterministic
- fast
- safe to run repeatedly
- focused on our logic rather than network availability

## What to Test When Changing Behavior

### Snapshot Planning

If you change:

- wildcard normalization
- timestamp filters
- all-timestamps behavior
- composite snapshot logic

then add or update planner/archive tests.

### Path Handling

If you change:

- query-string filename hashing
- Windows path sanitizing
- directory-vs-file mapping
- blocking-file restructuring

then add or update path and rewrite tests.

### Download Orchestration

If you change:

- resume logic
- page requisites
- worker sleeps
- state-file cleanup

then add or update downloader tests using temporary directories and mocks.

### Rewriting

If you change:

- HTML attribute rewriting
- CSS `url(...)` rewriting
- JavaScript rewriting
- subdomain local rewrites

then add regression tests with realistic input content.

## Common Implementation Notes

### Keep Network Access Behind the Transport

`ArchiveClient` should stay easy to fake in tests. New HTTP behavior should go
through the transport abstraction instead of reaching into `http.client`
directly from other modules.

### Reuse Path Mapping Rules

If a change affects how URLs become filenames, make sure:

- downloader writes use the same logic
- local rewriting uses the same logic
- resume logic still resolves the same file ID back to the same path

`paths.py` is the shared source of truth for that.

### Preserve Resume Safety

The downloader assumes `.downloaded.txt` is advisory, not absolute truth.
Whenever you change state handling, keep these behaviors intact:

- missing files should be re-queued even if the DB contains the ID
- failed runs should usually keep state files
- successful runs should remove state files unless `--keep` is set

## Known Gaps

The biggest remaining gap is end-to-end live verification against
`web.archive.org`. The code is structured so that this can be added later as a
separate integration layer without weakening the fast offline unit suite.

## Suggested Next Improvements

- split the growing single test module into focused test files
- add fixture-based HTML/CSS/JS rewrite regression cases
- add a dedicated integration test layer behind an opt-in environment flag
- add packaging/publishing metadata beyond the current local development setup
