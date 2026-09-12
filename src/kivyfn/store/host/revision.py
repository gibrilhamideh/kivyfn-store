# src/kivyfn/store/host/revision.py
from __future__ import annotations

from collections.abc import Mapping

from kivyfn.store.error import StoreStateError


class SubscriptionRevision:
    """Track schema revisions for one subscription in one Store instance.

    Snapshot mappings include every dependency, even those at revision zero.
    A schema subscription has one entry; a query includes its root and joins.
    Event mappings contain only changed schemas and may include unrelated
    schemas, which this tracker ignores.

    current records revisions covered by initialization or accepted for
    delivery. It does not acknowledge receipt or application by the client.
    The owner accesses this tracker under the Store lock, checks Store
    instance identity, and discards the tracker when the subscription ends.
    Delivery failure requires disconnect and resubscription.
    """

    def __init__(self, revisions: Mapping[str, int]) -> None:
        snapshot = _copy_revisions(revisions)
        if not snapshot:
            raise StoreStateError("Subscription revisions cannot be empty")

        self._snapshot = snapshot
        self._current = dict(snapshot)

    @property
    def snapshot(self) -> dict[str, int]:
        """Return a copy of the latest initialization revisions."""
        return dict(self._snapshot)

    @property
    def current(self) -> dict[str, int]:
        """Return a copy of initialization and accepted delivery progress."""
        return dict(self._current)

    def hydrate(self, revisions: Mapping[str, int]) -> None:
        """Record a complete snapshot or a verified unchanged client cache.

        The owner supplies authoritative Store revisions from a consistent
        initialization boundary. These may jump forward, but must cover the
        same dependencies without moving any of them backwards.
        """
        snapshot = _copy_revisions(revisions)
        if snapshot.keys() != self._current.keys():
            raise StoreStateError("Subscription dependencies cannot change")

        if any(
            snapshot[schema] < value for schema, value in self._current.items()
        ):
            raise StoreStateError("Snapshot revisions cannot move backwards")

        self._snapshot = snapshot
        self._current = dict(snapshot)

    def covers(self, revisions: Mapping[str, int]) -> bool:
        """Whether all relevant changes are already covered, without advancing.

        Capture can run ahead of queued delivery, so this method does not
        check for gaps. An empty or unrelated event is also covered.
        """
        relevant = self._relevant(revisions)
        return all(
            value <= self._current[schema]
            for schema, value in relevant.items()
        )

    def accept(self, revisions: Mapping[str, int]) -> bool:
        """Accept one commit's relevant changes immediately before delivery.

        Ignore fully covered events. Every relevant changed schema in a new
        event must advance exactly once. Gaps or partly covered commits
        raise StoreStateError without advancing any schema; the owner must
        recover rather than deliver a partial query or transaction update.
        """
        relevant = self._relevant(revisions)
        if all(
            value <= self._current[schema]
            for schema, value in relevant.items()
        ):
            return False

        for schema, value in relevant.items():
            expected = self._current[schema] + 1
            if value != expected:
                raise StoreStateError(
                    f"Subscription revision for {schema!r}: "
                    f"expected {expected}, got {value}"
                )

        self._current.update(relevant)
        return True

    def _relevant(self, revisions: Mapping[str, int]) -> dict[str, int]:
        values = _copy_revisions(revisions)
        return {
            schema: value
            for schema, value in values.items()
            if schema in self._current
        }


def _copy_revisions(revisions: Mapping[str, int]) -> dict[str, int]:
    """Validate and detach schema counters before updating progress."""
    values = dict(revisions)
    for schema, value in values.items():
        if not isinstance(schema, str) or not schema:
            raise StoreStateError(
                "Revision schema names must be non-empty strings"
            )
        if type(value) is not int or value < 0:
            raise StoreStateError(
                "Schema revisions must be non-negative integers"
            )

    return values
