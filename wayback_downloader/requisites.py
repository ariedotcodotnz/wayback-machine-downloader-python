from __future__ import annotations

import re


class PageRequisitesExtractor:
    ASSET_REGEX = re.compile(
        r"""(?:href|src|data-src|data-url)\s*=\s*["']([^"']+)["']|"""
        r"""url\(\s*["']?([^"')]+)["']?\s*\)|"""
        r"""srcset\s*=\s*["']([^"']+)["']""",
        re.IGNORECASE,
    )

    @classmethod
    def extract(cls, content: str) -> list[str]:
        assets: list[str] = []
        for match in cls.ASSET_REGEX.findall(content):
            candidate = next((item for item in match if item), None)
            if not candidate:
                continue
            if "," in candidate and (" 1x" in candidate or " 2w" in candidate):
                for part in candidate.split(","):
                    asset = part.strip().split(" ", 1)[0]
                    if cls._valid_asset(asset):
                        assets.append(asset)
                continue
            if cls._valid_asset(candidate):
                assets.append(candidate)
        return list(dict.fromkeys(assets))

    @staticmethod
    def _valid_asset(url: str) -> bool:
        stripped = url.strip()
        if not stripped:
            return False
        return not stripped.startswith(("data:", "mailto:", "#", "javascript:"))
