# src/kivyfn/store/host/query.py
from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from kivyfn.store.error import InvalidOperationError, StoreStateError
from kivyfn.store.protocol.message.event import QueryEvent
from kivyfn.store.query import Query, QueryResult


if TYPE_CHECKING:
    from kivyfn.store.query.plan import QueryPlan
    from kivyfn.store.query.shape import QueryShape
    from kivyfn.store.store import Store


class HostQuery:
    """One shared live query with results captured at a Store revision.

    The owner holds the Store lock while creating or capturing this query.
    The supplied revision must describe the state being read. Capture runs
    during commit processing, before any asynchronous client delivery.
    """

    def __init__(self, store: Store, plan: QueryPlan, revision: int) -> None:
        self._store = store
        self._plan = plan
        self._revision = revision
        self._references = 0
        self._dependencies = frozenset(
            {
                plan.shape.root_schema,
                *(join.target_schema for join in plan.shape.joins),
            }
        )
        rows = store.execute_query(plan)
        self._rows = {row.id: row for row in rows}
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
    def revision(self) -> int:
        return self._revision

    @property
    def references(self) -> int:
        return self._references

    @property
    def rows(self) -> QueryResult:
        return tuple(self._rows.values())

    def capture(self, revision: int) -> None:
        """Evaluate once per revision and retain its changes for all users.

        Repeated capture of the same revision preserves the cached changes.
        Subscriptions decide whether their snapshot already covers them.
        """
        if revision < self._revision:
            raise StoreStateError("Live query capture cannot move backwards")

        if revision == self._revision:
            return

        if revision != self._store.revision:
            raise StoreStateError("Store advanced before query capture")

        rows = self._store.execute_query(self._plan)
        current = {row.id: row for row in rows}
        changed = tuple(
            row for id, row in current.items() if row != self._rows.get(id)
        )
        removed = tuple(id for id in self._rows if id not in current)

        self._rows = current
        self._changed = changed
        self._removed = removed
        self._revision = revision

    def event(self, subscription_id: str) -> QueryEvent | None:
        """Bind the latest captured changes to one subscription."""
        if not self._changed and not self._removed:
            return None

        return QueryEvent(subscription_id, self._changed, self._removed)


class HostQueries:
    """Share live queries across subscriptions to the same Store.

    The subscription owner balances each acquire() with one release().
    Access runs under the Store lock. A fresh snapshot reads the acquired
    query's rows and revision together with subscription registration.
    """

    def __init__(self, store: Store) -> None:
        self._store = store
        self._queries: dict[Query[Any], HostQuery] = {}

    def acquire(self, query: Query[Any], revision: int) -> HostQuery:
        """Acquire a live plan and a current snapshot of its result."""
        if not self._store.booted:
            raise StoreStateError("Store is unavailable")

        query = _normalize(query)
        current = self._queries.get(query)

        if current is None:
            plan = self._store.compile_query(query)

            if not plan.live_compatible:
                raise InvalidOperationError("Query must preserve row identity")

            current = HostQuery(self._store, plan, revision)
            self._queries[query] = current
        else:
            current.capture(revision)

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
