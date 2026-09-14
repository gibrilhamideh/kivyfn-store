# src/kivyfn/store/host/query.py
from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from kivyfn.store.error import InvalidOperationError, StoreStateError
from kivyfn.store.protocol.message.event import (
    EventAction,
    InsertEventAction,
    RemoveEventAction,
    SetRowEventAction,
)
from kivyfn.store.query import Query, QueryResult, QueryRow


if TYPE_CHECKING:
    from kivyfn.store.query.plan import QueryPlan
    from kivyfn.store.store import Store


class HostQuery:
    """One shared live query and its current canonical result."""

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

        revisions = store.schema_revisions

        self._revisions = {
            schema: revisions[schema] for schema in self._dependencies
        }

        self._rows = self._execute()

    @property
    def source(self) -> Query[Any]:
        return self._plan.query

    @property
    def dependencies(self) -> frozenset[str]:
        return self._dependencies

    @property
    def revisions(self) -> dict[str, int]:
        return dict(self._revisions)

    @property
    def references(self) -> int:
        return self._references

    @property
    def rows(self) -> QueryResult:
        return tuple(self._rows.values())

    def capture(
        self, revisions: dict[str, int]
    ) -> tuple[EventAction, ...] | None:
        relevant = {
            schema: revision
            for schema, revision in revisions.items()
            if schema in self._dependencies
        }

        if not relevant:
            return None

        for schema, revision in relevant.items():
            expected = self._revisions[schema] + 1

            if revision != expected:
                raise StoreStateError(
                    f"Query revision for {schema!r}: "
                    f"expected {expected}, got {revision}"
                )

        current = self._execute()
        actions = _diff_rows(self._rows, current)

        self._rows = current
        self._revisions.update(relevant)

        return actions

    def _execute(self) -> dict[str, QueryRow]:
        return {row.id: row for row in self._store.execute_query(self._plan)}


class HostQueries:
    """Share one live HostQuery between equivalent subscriptions."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._queries: dict[Query[Any], HostQuery] = {}

    def acquire(self, query: Query[Any]) -> HostQuery:
        query = _normalize(query)
        current = self._queries.get(query)

        if current is None:
            plan = self._store.compile_query(query)

            if not plan.live_compatible:
                raise InvalidOperationError("Query must preserve row identity")

            current = HostQuery(self._store, plan)

            self._queries[query] = current

        current._references += 1
        return current

    def release(self, query: HostQuery) -> None:
        if self._queries.get(query.source) is not query:
            return

        query._references -= 1

        if query.references == 0:
            self._queries.pop(query.source, None)

    def clear(self) -> None:
        self._queries.clear()

    def __len__(self) -> int:
        return len(self._queries)


def _diff_rows(
    previous: dict[str, QueryRow], current: dict[str, QueryRow]
) -> tuple[EventAction, ...]:
    actions: list[EventAction] = []

    for row_id in previous:
        if row_id not in current:
            actions.append(RemoveEventAction(row_id=row_id))

    for row_id, row in current.items():
        old = previous.get(row_id)

        if old is None:
            actions.append(
                InsertEventAction(
                    row_id=row_id, values=tuple(dict(row).items())
                )
            )

            continue

        if row != old:
            actions.append(
                SetRowEventAction(
                    row_id=row_id, values=tuple(dict(row).items())
                )
            )

    return tuple(actions)


def _normalize(query: Query[Any]) -> Query[Any]:
    schema = query.schema_name

    if schema is None:
        raise InvalidOperationError(
            "Live query subscriptions require a schema name or class"
        )

    return replace(query, source=schema)
