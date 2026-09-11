# src/kivyfn/store/journal.py
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from kivyfn.store.events.event import StoreCommit


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """One canonical Store commit at a global Store revision."""

    revision: int
    commit: StoreCommit


class CommitJournal:
    """Bounded recent history of canonical Store commits."""

    def __init__(self, max_entries: int) -> None:
        if (
            isinstance(max_entries, bool)
            or not isinstance(max_entries, int)
            or max_entries <= 0
        ):
            raise ValueError("Journal max entries must be a positive integer")

        self._max_entries = max_entries
        self._revision = 0
        self._entries: deque[JournalEntry] = deque(maxlen=max_entries)

    @property
    def max_entries(self) -> int:
        return self._max_entries

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def oldest_revision(self) -> int | None:
        if not self._entries:
            return None

        return self._entries[0].revision

    @property
    def newest_revision(self) -> int | None:
        if not self._entries:
            return None

        return self._entries[-1].revision

    def record(self, commit: StoreCommit) -> JournalEntry:
        """Record one successful canonical commit."""
        if not isinstance(commit, StoreCommit):
            raise TypeError("Journal entries must contain a StoreCommit")

        self._revision += 1

        entry = JournalEntry(revision=self._revision, commit=commit)
        self._entries.append(entry)

        return entry

    def replay_after(self, revision: int) -> tuple[JournalEntry, ...] | None:
        """
        Return entries after revision, or None when the gap expired.

        An empty tuple means the caller is already current.
        """
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 0
        ):
            raise ValueError("Store revision must be a non-negative integer")

        if revision > self._revision:
            return None

        if revision == self._revision:
            return ()

        oldest = self.oldest_revision

        if oldest is None:
            return None

        if revision + 1 < oldest:
            return None

        return tuple(
            entry for entry in self._entries if entry.revision > revision
        )

    def __len__(self) -> int:
        return len(self._entries)
