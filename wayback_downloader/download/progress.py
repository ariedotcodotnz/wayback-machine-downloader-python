from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field


@dataclass(slots=True)
class DownloadProgress:
    """Thread-safe progress counter and logger for download workers."""

    total: int = 0
    completed: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def reset(self, total: int) -> None:
        with self._lock:
            self.total = total
            self.completed = 0

    def add_total(self, count: int = 1) -> None:
        with self._lock:
            self.total += count

    def mark_completed(
        self,
        status: str,
        url: str,
        logger: logging.Logger,
        *,
        error: str | None = None,
    ) -> None:
        with self._lock:
            self.completed += 1
            message = f"[{status}] {url} ({self.completed}/{self.total})"
            if error:
                message = f"{message} {error}"
            logger.info(message)
