# src/kivyfn/store/client/queue.py
from __future__ import annotations

from collections import deque

from kivyfn.store.protocol.message.requests import RequestBatch, RequestItem


class RequestQueue:
    """Collect request items and queue complete request batches."""

    def __init__(self) -> None:
        self._current: list[RequestItem] = []
        self._batches: deque[RequestBatch] = deque()

    @property
    def empty(self) -> bool:
        return not self._current and not self._batches

    @property
    def has_current(self) -> bool:
        return bool(self._current)

    @property
    def has_batches(self) -> bool:
        return bool(self._batches)

    def append(self, request: RequestItem) -> None:
        self._current.append(request)

    def batch(self) -> RequestBatch | None:
        """Create one batch from requests collected this event-loop turn."""
        if not self._current:
            return None

        batch = RequestBatch(items=tuple(self._current))

        self._current.clear()
        self._batches.append(batch)

        return batch

    def popleft(self) -> RequestBatch | None:
        """Take the oldest queued batch."""
        if not self._batches:
            return None

        return self._batches.popleft()

    def appendleft(self, batch: RequestBatch) -> None:
        """Put a batch back at the front of the queue."""
        self._batches.appendleft(batch)

    def clear(self) -> None:
        self._current.clear()
        self._batches.clear()

    def __len__(self) -> int:
        return len(self._batches)
