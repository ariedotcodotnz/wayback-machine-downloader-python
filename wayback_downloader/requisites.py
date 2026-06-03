from __future__ import annotations

import re


# Matches one srcset descriptor: a positive number followed by ``w`` (width)
# or ``x`` (density). The old heuristic only looked for the literal strings
# ``" 1x"`` and ``" 2w"`` — fine for hand-written examples, but WordPress
# generates descriptors like ``300w``, ``768w``, ``1536w``, ``2000w`` and the
# old check failed to recognize them, so the entire srcset value (multiple
# URLs joined by commas) got treated as one giant URL.
_SRCSET_DESCRIPTOR = re.compile(r"^\d+(?:\.\d+)?[wx]$", re.IGNORECASE)


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
            srcset_parts = cls._split_srcset(candidate)
            if srcset_parts is not None:
                for part in srcset_parts:
                    if cls._valid_asset(part):
                        assets.append(part)
                continue
            if cls._valid_asset(candidate):
                assets.append(candidate)
        return list(dict.fromkeys(assets))

    @staticmethod
    def _split_srcset(candidate: str) -> list[str] | None:
        """Return the URLs in a srcset value, or ``None`` if it isn't a srcset.

        A srcset is comma-separated and each part ends in a width (``\\d+w``)
        or density (``\\d+x``) descriptor. We require *every* non-empty part
        to look like that, so URLs that merely contain commas (rare but
        possible in query strings) aren't misclassified.
        """

        if "," not in candidate:
            return None
        parts = [part.strip() for part in candidate.split(",") if part.strip()]
        if len(parts) < 2:
            return None
        urls: list[str] = []
        for part in parts:
            url, descriptor = (part.rsplit(" ", 1) + [""])[:2] if " " in part else (part, "")
            if not _SRCSET_DESCRIPTOR.match(descriptor):
                return None
            urls.append(url.strip())
        return urls

    @staticmethod
    def _valid_asset(url: str) -> bool:
        stripped = url.strip()
        if not stripped:
            return False
        return not stripped.startswith(("data:", "mailto:", "#", "javascript:"))
