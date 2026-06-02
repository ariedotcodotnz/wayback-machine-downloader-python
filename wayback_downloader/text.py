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
    current = raw_text
    while True:
        decoded = unquote_to_bytes(current)
        round_trip = decoded.decode("latin-1")
        if round_trip == current:
            return decoded
        current = round_trip
