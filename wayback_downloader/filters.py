from __future__ import annotations

import re
from dataclasses import dataclass, field


_SLASH_REGEX = re.compile(r"^/(.*)/([imx]*)$", re.DOTALL)
_PERCENT_REGEX = re.compile(r"^%r\{(.*)\}([imx]*)$", re.DOTALL)


def _compile_literal_or_regex(pattern: str | None) -> tuple[str, str | re.Pattern[str]] | None:
    if not pattern:
        return None
    for candidate in (_SLASH_REGEX, _PERCENT_REGEX):
        match = candidate.match(pattern)
        if match:
            flags = 0
            inline_flags = match.group(2)
            if "i" in inline_flags:
                flags |= re.IGNORECASE
            if "m" in inline_flags:
                flags |= re.MULTILINE
            if "x" in inline_flags:
                flags |= re.VERBOSE
            return ("regex", re.compile(match.group(1), flags))
    return ("literal", pattern.casefold())


@dataclass(slots=True)
class URLFilter:
    include_pattern: str | None = None
    exclude_pattern: str | None = None
    _include: tuple[str, str | re.Pattern[str]] | None = field(init=False, repr=False)
    _exclude: tuple[str, str | re.Pattern[str]] | None = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._include = _compile_literal_or_regex(self.include_pattern)
        self._exclude = _compile_literal_or_regex(self.exclude_pattern)

    def allows(self, url: str) -> bool:
        return self.matches_include(url) and not self.matches_exclude(url)

    def matches_include(self, url: str) -> bool:
        return self._matches(self._include, url, default=True)

    def matches_exclude(self, url: str) -> bool:
        return self._matches(self._exclude, url, default=False)

    @staticmethod
    def _matches(
        compiled: tuple[str, str | re.Pattern[str]] | None,
        url: str,
        *,
        default: bool,
    ) -> bool:
        if compiled is None:
            return default
        kind, value = compiled
        if kind == "literal":
            return str(value) in url.casefold()
        return bool(value.search(url))
