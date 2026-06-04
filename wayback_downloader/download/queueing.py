from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Iterable

from ..models import Snapshot


SnapshotJobQueue = queue.Queue[Snapshot | None]
SnapshotProcessor = Callable[[Snapshot, SnapshotJobQueue], bool]


class SnapshotDownloadQueue:
    """Run snapshot downloads across a bounded worker pool."""

    def __init__(
        self,
        *,
        worker_count: int,
        rate_limit: float,
        processor: SnapshotProcessor,
    ) -> None:
        self.worker_count = max(1, worker_count)
        self.rate_limit = rate_limit
        self.processor = processor
        self.jobs: SnapshotJobQueue = queue.Queue()

    def run(self, initial_jobs: Iterable[Snapshot]) -> None:
        workers = [
            threading.Thread(target=self._worker, name=f"wayback-worker-{index}", daemon=True)
            for index in range(self.worker_count)
        ]
        for worker in workers:
            worker.start()

        for snapshot in initial_jobs:
            self.jobs.put(snapshot)

        self.jobs.join()
        for _ in workers:
            self.jobs.put(None)
        for worker in workers:
            worker.join()

    def _worker(self) -> None:
        while True:
            snapshot = self.jobs.get()
            if snapshot is None:
                self.jobs.task_done()
                return

            network_used = False
            try:
                network_used = self.processor(snapshot, self.jobs)
            finally:
                self.jobs.task_done()
                if network_used:
                    time.sleep(self.rate_limit)
