from __future__ import annotations

from urllib.parse import unquote_to_bytes


COMMON_ENCODINGS = (
    "utf-8",
    "cp1251",
    "gb18030",
    "shift_jis",
    "euc_kr",
    "iso-8859-1",
    "cp1252",
)


def decode_best_effort(raw: bytes) -> str:
    text, _ = decode_with_candidates(raw)
    return text


def decode_with_candidates(raw: bytes, preferred: str | None = None) -> tuple[str, str]:
    candidates: list[str] = []
    if preferred:
        candidates.append(preferred)
    candidates.extend(COMMON_ENCODINGS)

    seen: set[str] = set()
    for encoding in candidates:
        normalized = encoding.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        try:
            return raw.decode(encoding), encoding
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8"


def repeated_percent_decode(raw_text: str) -> bytes:
    """Percent-decode until no more ``%XX`` escapes remain.

    Wayback occasionally archives URLs that have been percent-encoded multiple
    times (``%2520`` -> ``%20`` -> space), so we unwrap iteratively. The earlier
    implementation tested for a fixed point via a ``latin-1`` round trip, which
    silently ran away on any input containing non-ASCII characters: each loop
    re-interpreted the UTF-8 bytes as several latin-1 chars, growing the string
    until ``unquote_to_bytes`` ran out of memory. We now stop as soon as there
    is nothing left to decode, decode bytes as UTF-8 when valid (returning the
    raw bytes for legacy-encoded paths like cp1251), and cap the loop so a
    pathological input cannot spin forever.
    """

    current = raw_text
    for _ in range(8):
        if "%" not in current:
            return current.encode("utf-8", errors="replace")
        decoded = unquote_to_bytes(current)
        try:
            next_text = decoded.decode("utf-8")
        except UnicodeDecodeError:
            # Decoded bytes aren't valid UTF-8 (e.g. cp1251 or shift_jis path);
            # hand them to the caller who will sniff the right encoding.
            return decoded
        if next_text == current:
            return decoded
        current = next_text
    return current.encode("utf-8", errors="replace")
