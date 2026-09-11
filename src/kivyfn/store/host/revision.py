# src/kivyfn/store/host/revision.py
from __future__ import annotations

from kivyfn.store.error import StoreStateError


class SubscriptionRevision:
    """Track one subscription's snapshot and accepted event revisions.

    current is the newest revision covered by a snapshot or accepted for
    delivery. It is not a remote acknowledgement. Delivery failure requires
    disconnect and resubscription, with a new tracker for a new Store.

    Revisions may skip unrelated Store commits. The subscription owner
    accesses this tracker under the Store lock.
    """

    def __init__(self, revision: int) -> None:
        self._snapshot = revision
        self._current = revision

    @property
    def snapshot(self) -> int:
        return self._snapshot

    @property
    def current(self) -> int:
        return self._current

    def hydrate(self, revision: int) -> None:
        """Record a new snapshot and cover older queued changes."""
        if revision < self._current:
            raise StoreStateError("Snapshot revision cannot move backwards")

        self._snapshot = revision
        self._current = revision

    def covers(self, revision: int) -> bool:
        """Whether this revision is already covered by the subscription."""
        return revision <= self._current

    def accept(self, revision: int) -> bool:
        """Accept an uncovered revision for delivery exactly once."""
        if self.covers(revision):
            return False

        self._current = revision
        return True
