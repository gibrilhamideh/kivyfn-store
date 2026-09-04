from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from typing import Any, TypeAlias

from kivyfn.store.error import StoreProtocolError


QueryRow = dict[str, Any]
QueryResult = dict[str, QueryRow]


class QueryEventKind(str, Enum):
    INSERT = "insert"
    UPDATE = "update"
    REMOVE = "remove"


@dataclass(frozen=True, slots=True)
class QueryInsertEvent:
    id: str
    new_value: QueryRow
    kind: QueryEventKind = QueryEventKind.INSERT

    def payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "id": self.id,
            "new_value": deepcopy(self.new_value),
        }


@dataclass(frozen=True, slots=True)
class QueryUpdateEvent:
    id: str
    old_value: QueryRow
    new_value: QueryRow
    kind: QueryEventKind = QueryEventKind.UPDATE

    def payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "id": self.id,
            "old_value": deepcopy(self.old_value),
            "new_value": deepcopy(self.new_value),
        }


@dataclass(frozen=True, slots=True)
class QueryRemoveEvent:
    id: str
    old_value: QueryRow
    kind: QueryEventKind = QueryEventKind.REMOVE

    def payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "id": self.id,
            "old_value": deepcopy(self.old_value),
        }


QueryEvent: TypeAlias = (
    QueryInsertEvent
    | QueryUpdateEvent
    | QueryRemoveEvent
)


@dataclass(frozen=True, slots=True)
class QueryChangeSet:
    sequence: int
    events: tuple[QueryEvent, ...]
    server_instance_id: str | None = None

    def payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "sequence": self.sequence,
            "events": [event.payload() for event in self.events],
        }

        if self.server_instance_id is not None:
            payload["server_instance_id"] = self.server_instance_id

        return payload

    @classmethod
    def from_payload(cls, payload: Any) -> QueryChangeSet:
        if not isinstance(payload, dict):
            raise StoreProtocolError(
                "Query change set must be an object"
            )

        sequence = payload.get("sequence")
        events = payload.get("events")
        instance_id = payload.get("server_instance_id")

        if isinstance(sequence, bool) or not isinstance(sequence, int):
            raise StoreProtocolError(
                "Query change sequence must be an integer"
            )

        if sequence < 0:
            raise StoreProtocolError(
                "Query change sequence cannot be negative"
            )

        if not isinstance(events, list):
            raise StoreProtocolError(
                "Query change events must be a list"
            )

        if instance_id is not None:
            if not isinstance(instance_id, str) or not instance_id:
                raise StoreProtocolError(
                    "Query change server instance ID is invalid"
                )

        return cls(
            server_instance_id=instance_id,
            sequence=sequence,
            events=tuple(
                query_event_from_payload(event)
                for event in events
            ),
        )


def query_event_from_payload(payload: Any) -> QueryEvent:
    if not isinstance(payload, dict):
        raise StoreProtocolError(
            "Query event must be an object"
        )

    try:
        kind = QueryEventKind(payload.get("kind"))
    except ValueError as exc:
        raise StoreProtocolError(
            "Query event kind is invalid"
        ) from exc

    row_id = payload.get("id")

    if not isinstance(row_id, str) or not row_id:
        raise StoreProtocolError(
            "Query event row ID is invalid"
        )

    if kind is QueryEventKind.INSERT:
        return QueryInsertEvent(
            id=row_id,
            new_value=_row(payload.get("new_value"), "new_value"),
        )

    if kind is QueryEventKind.UPDATE:
        return QueryUpdateEvent(
            id=row_id,
            old_value=_row(payload.get("old_value"), "old_value"),
            new_value=_row(payload.get("new_value"), "new_value"),
        )

    return QueryRemoveEvent(
        id=row_id,
        old_value=_row(payload.get("old_value"), "old_value"),
    )


def diff_query_results(
    previous: QueryResult,
    current: QueryResult,
) -> tuple[QueryEvent, ...]:
    events: list[QueryEvent] = []
    previous_ids = set(previous)
    current_ids = set(current)

    for row_id in sorted(previous_ids - current_ids):
        events.append(
            QueryRemoveEvent(
                id=row_id,
                old_value=deepcopy(previous[row_id]),
            )
        )

    for row_id in sorted(current_ids - previous_ids):
        events.append(
            QueryInsertEvent(
                id=row_id,
                new_value=deepcopy(current[row_id]),
            )
        )

    for row_id in sorted(previous_ids & current_ids):
        old_value = previous[row_id]
        new_value = current[row_id]

        if old_value == new_value:
            continue

        events.append(
            QueryUpdateEvent(
                id=row_id,
                old_value=deepcopy(old_value),
                new_value=deepcopy(new_value),
            )
        )

    return tuple(events)


def copy_query_result(
    value: Mapping[str, Mapping[str, Any]],
) -> QueryResult:
    return {
        row_id: deepcopy(dict(row))
        for row_id, row in value.items()
    }


def apply_query_change(
    data: QueryResult,
    change: QueryChangeSet,
) -> QueryResult:
    updated = copy_query_result(data)

    for event in change.events:
        if isinstance(event, QueryInsertEvent):
            updated[event.id] = deepcopy(event.new_value)
            continue

        if isinstance(event, QueryUpdateEvent):
            updated[event.id] = deepcopy(event.new_value)
            continue

        updated.pop(event.id, None)

    return updated


def _row(value: Any, name: str) -> QueryRow:
    if not isinstance(value, dict):
        raise StoreProtocolError(
            f"Query event {name} must be an object"
        )

    return deepcopy(value)
