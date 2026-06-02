# wayback-downloader

An object-oriented Python rewrite of the Ruby Wayback Machine downloader.

## Features

- Download the latest capture of each file for a site
- Download every timestamped capture
- Build a composite snapshot for a point in time
- Resume downloads with `.cdx.json` and `.downloaded.txt`
- Rewrite archived links for local browsing
- Queue page requisites like CSS, JS, and images
- Recursively discover and download subdomains

## Quick start

```bash
python -m wayback_downloader https://example.com
```

List files without downloading:

```bash
python -m wayback_downloader --list https://example.com
```

Rewrite an existing download for local browsing:

```bash
python -m wayback_downloader --local-only ./websites/example.com
```
