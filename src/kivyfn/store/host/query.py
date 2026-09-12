# src/kivyfn/store/host/query.py
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from kivyfn.store.error import InvalidOperationError, StoreStateError
from kivyfn.store.protocol.message.event import QueryEvent
from kivyfn.store.query import Query, QueryResult, QueryRow


if TYPE_CHECKING:
    from kivyfn.store.query.plan import QueryPlan
    from kivyfn.store.query.shape import QueryShape
    from kivyfn.store.store import Store


class HostQuery:
    """One shared live query with results tied to schema revisions.

    The owner holds Store.synchronized() while accessing this object. Pair
    rows and schema_revisions with subscription registration in that scope.
    Capture runs during commit processing, before asynchronous delivery.
    Only the latest result and its most recent changes are retained.
    """

    def __init__(self, store: Store, plan: QueryPlan) -> None:
        self._store = store
        self._plan = plan
        self._references = 0
        self._dependencies = frozenset(
            {
                plan.shape.root_schema,
                *(join.target_schema for join in plan.shape.joins),
            }
        )
        self._schema_revisions = self._read_revisions()
        self._rows = self._capture_rows(self._schema_revisions)
        self._last_revisions: dict[str, int] = {}
        self._changed: QueryResult = ()
        self._removed: tuple[str, ...] = ()

    @property
    def source(self) -> Query[Any]:
        return self._plan.query

    @property
    def shape(self) -> QueryShape:
        return self._plan.shape

    @property
    def dependencies(self) -> frozenset[str]:
        return self._dependencies

    @property
    def schema_revisions(self) -> dict[str, int]:
        """Return a copy of all dependency revisions for these rows."""
        return dict(self._schema_revisions)

    @property
    def references(self) -> int:
        return self._references

    @property
    def rows(self) -> QueryResult:
        return tuple(self._rows.values())

    def capture(self, revisions: Mapping[str, int]) -> bool:
        """Capture one commit's changed dependency schemas for all subscribers.

        Return True when event() describes this commit, including repeated
        calls for it. Return False for unrelated or older covered commits;
        callers must not attach the previous changes to those messages.

        Changed dependencies must advance once and describe the current
        Store data. Missing or partly covered changes raise StoreStateError.
        No rows, revisions, or cached changes advance if capture fails.
        """
        relevant = {
            schema: value
            for schema, value in revisions.items()
            if schema in self._dependencies
        }
        if not relevant:
            return False

        for value in relevant.values():
            if type(value) is not int or value < 0:
                raise StoreStateError(
                    "Query revisions must be non-negative integers"
                )

        if relevant == self._last_revisions:
            return True

        if all(
            value <= self._schema_revisions[schema]
            for schema, value in relevant.items()
        ):
            return False

        for schema, value in relevant.items():
            expected = self._schema_revisions[schema] + 1
            if value != expected:
                raise StoreStateError(
                    f"Query revision for {schema!r}: "
                    f"expected {expected}, got {value}"
                )

        target = {**self._schema_revisions, **relevant}
        current = self._capture_rows(target)
        changed = tuple(
            row for id, row in current.items() if row != self._rows.get(id)
        )
        removed = tuple(id for id in self._rows if id not in current)

        self._rows = current
        self._changed = changed
        self._removed = removed
        self._schema_revisions = target
        self._last_revisions = relevant
        return True

    def event(self, subscription_id: str) -> QueryEvent | None:
        """Bind changes after capture() returns True for the enclosing commit.

        None means the result rows did not change. The owner must still send
        the commit's changed dependency revisions to covered subscriptions.
        """
        if not self._changed and not self._removed:
            return None

        return QueryEvent(subscription_id, self._changed, self._removed)

    def _read_revisions(self) -> dict[str, int]:
        revisions = self._store.schema_revisions
        return {schema: revisions[schema] for schema in self._dependencies}

    def _capture_rows(
        self, revisions: Mapping[str, int]
    ) -> dict[str, QueryRow]:
        if self._read_revisions() != revisions:
            raise StoreStateError("Store advanced before query capture")

        rows = self._store.execute_query(self._plan)

        if self._read_revisions() != revisions:
            raise StoreStateError("Store changed during query capture")

        return {row.id: row for row in rows}


class HostQueries:
    """Share live queries across subscriptions to the same Store.

    The subscription owner balances each acquire() with one release().
    Access runs under the Store lock. A fresh snapshot reads the acquired
    query's rows and schema revisions with subscription registration.
    """

    def __init__(self, store: Store) -> None:
        self._store = store
        self._queries: dict[Query[Any], HostQuery] = {}

    def acquire(self, query: Query[Any]) -> HostQuery:
        """Acquire a current result without discarding pending commit changes.

        Existing queries must have captured all dependency changes already.
        Refreshing one here could skip events still needed by its subscribers.
        """
        if not self._store.booted:
            raise StoreStateError("Store is unavailable")

        query = _normalize(query)
        current = self._queries.get(query)

        if current is None:
            plan = self._store.compile_query(query)

            if not plan.live_compatible:
                raise InvalidOperationError("Query must preserve row identity")

            current = HostQuery(self._store, plan)
            self._queries[query] = current
        elif current.schema_revisions != current._read_revisions():
            raise StoreStateError("Live query has uncaptured schema changes")

        current._references += 1
        return current

    def get(self, query: Query[Any]) -> HostQuery | None:
        return self._queries.get(_normalize(query))

    def release(self, query: HostQuery) -> None:
        """Release one owner; discard the query after its final release."""
        if self._queries.get(query.source) is not query:
            return

        query._references -= 1

        if query.references == 0:
            del self._queries[query.source]

    def clear(self) -> None:
        """Release all shared plans when the host stops."""
        for query in self._queries.values():
            query._references = 0

        self._queries.clear()

    def __len__(self) -> int:
        return len(self._queries)


def _normalize(query: Query[Any]) -> Query[Any]:
    schema = query.schema_name

    if schema is None:
        raise InvalidOperationError(
            "Live query subscriptions require a schema name or class"
        )

    return replace(query, source=schema)
