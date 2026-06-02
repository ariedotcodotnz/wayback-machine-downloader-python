from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit


class SubdomainDiscovery:
    @staticmethod
    def extract_base_domain(url: str) -> str | None:
        candidate = url if "://" in url else f"https://{url}"
        parsed = urlsplit(candidate)
        host = (parsed.hostname or "").lower()
        if not host:
            return None
        parts = host.split(".")
        if len(parts) <= 2:
            return host
        if len(parts[-2]) <= 3 and len(parts[-1]) <= 3 and len(parts) > 2:
            return ".".join(parts[-3:])
        return ".".join(parts[-2:])

    @staticmethod
    def scan_files(files: list[Path], base_domain: str) -> list[str]:
        subdomains: set[str] = set()
        escaped_domain = re.escape(base_domain)
        patterns = (
            re.compile(rf"""(?:href|src|action|data-src)=["']https?://([^/."']+)\.{escaped_domain}[/"]""", re.IGNORECASE),
            re.compile(rf"""url\(["']?https?://([^/."']+)\.{escaped_domain}[/"]""", re.IGNORECASE),
            re.compile(rf"""["']https?://([^/."']+)\.{escaped_domain}[/"]""", re.IGNORECASE),
        )
        for file_path in files:
            if not file_path.exists():
                continue
            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for pattern in patterns:
                for match in pattern.findall(content):
                    lowered = match.lower()
                    if lowered != "www":
                        subdomains.add(lowered)
        return sorted(subdomains)
