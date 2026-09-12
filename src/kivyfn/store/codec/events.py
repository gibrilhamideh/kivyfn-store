# src/kivyfn/store/codec/events.py
from __future__ import annotations

from typing import Any, assert_never

from kivyfn.store.codec.query import decode_query_rows, encode_query_rows
from kivyfn.store.events import (
    InsertEvent,
    RemoveEvent,
    SetEvent,
    SetRowEvent,
    StoreCommit,
    StoreEvent,
)
from kivyfn.store.operations import OperationSource
from kivyfn.store.protocol.message.event import EventMessage, QueryEvent


def encode_event(event: StoreEvent) -> dict[str, Any]:
    """Encode one schema-based Store event."""
    payload: dict[str, Any] = {
        "type": event.opcode.value,
        "schema": event.schema,
        "source": event.source.value,
    }

    match event:
        case SetEvent():
            payload.update(key=event.key, old=event.old, new=event.new)
        case SetRowEvent():
            payload.update(
                row_id=event.id, key=event.key, old=event.old, new=event.new
            )
        case InsertEvent():
            payload.update(row_id=event.id, new=dict(event.new))
        case RemoveEvent():
            payload.update(row_id=event.id, old=dict(event.old))
        case _:
            assert_never(event)

    return payload


def decode_event(payload: object) -> StoreEvent:
    """Decode a Store event, retaining explicit null field values."""
    data = _object(payload, "Store event")
    kind = _string(data, "type")
    schema = _string(data, "schema")
    source = OperationSource(_string(data, "source"))

    match kind:
        case "set":
            return SetEvent(
                schema=schema,
                source=source,
                key=_string(data, "key"),
                old=_required(data, "old"),
                new=_required(data, "new"),
            )
        case "setrow":
            return SetRowEvent(
                schema=schema,
                source=source,
                id=_string(data, "row_id"),
                key=_string(data, "key"),
                old=_required(data, "old"),
                new=_required(data, "new"),
            )
        case "insert":
            return InsertEvent(
                schema=schema,
                source=source,
                id=_string(data, "row_id"),
                new=_row(data.get("new")),
            )
        case "remove":
            return RemoveEvent(
                schema=schema,
                source=source,
                id=_string(data, "row_id"),
                old=_row(data.get("old")),
            )
        case _:
            raise ValueError(f"Unknown Store event type: {kind!r}")


def encode_commit(
    commit: StoreCommit, *, require_stamp: bool = False
) -> dict[str, Any]:
    """Encode one atomic commit with independent schema revisions."""
    revisions = _schema_revisions(commit.schema_revisions)
    _validate_stamp(
        commit.server_instance_id,
        revisions,
        commit.events,
        require_stamp=require_stamp,
    )
    payload: dict[str, Any] = {
        "events": [encode_event(event) for event in commit.events]
    }

    if commit.operation_id is not None:
        payload["operation_id"] = commit.operation_id

    if commit.server_instance_id is not None:
        payload["server_instance_id"] = commit.server_instance_id
        payload["schema_revisions"] = revisions

    return payload


def decode_commit(
    payload: object, *, require_stamp: bool = False
) -> StoreCommit:
    """Decode a commit; network notifications require its Store stamp.

    Unstamped Engine results have no schema revisions. Stamped commits
    require an instance ID and a mapping covering every included event's
    schema. Extra entries are allowed for filtered query dependencies.
    Empty event lists are valid for filtered commits and successful no-ops.
    """
    data = _object(payload, "Store commit")

    if "revision" in data:
        raise ValueError("Global commit revisions are no longer supported")

    events = tuple(
        decode_event(event)
        for event in _list(data.get("events"), "commit events")
    )
    operation_id = _optional_string(data, "operation_id")
    instance_id = _optional_string(data, "server_instance_id")
    revisions = _schema_revisions(
        data.get("schema_revisions", {} if instance_id is None else None)
    )
    _validate_stamp(
        instance_id, revisions, events, require_stamp=require_stamp
    )

    return StoreCommit(
        events=events,
        operation_id=operation_id,
        server_instance_id=instance_id,
        schema_revisions=revisions,
    )


def encode_query_event(event: QueryEvent) -> dict[str, Any]:
    """Encode complete changed rows and IDs removed from one query."""
    return {
        "subscription_id": event.subscription_id,
        "rows": encode_query_rows(event.rows),
        "removed": list(event.removed),
    }


def decode_query_event(payload: object) -> QueryEvent:
    data = _object(payload, "query event")
    rows = decode_query_rows(data.get("rows", []))
    removed = _ids(data.get("removed", []), "removed query row IDs")

    if set(removed).intersection(row.id for row in rows):
        raise ValueError("A query event cannot change and remove the same row")

    return QueryEvent(
        subscription_id=_string(data, "subscription_id"),
        rows=rows,
        removed=removed,
    )


def encode_event_message(message: EventMessage) -> dict[str, Any]:
    """Encode one subscription notification as {"event": {...}}."""
    commit = encode_commit(message.commit, require_stamp=True)

    if not message.commit.schema_revisions:
        raise ValueError("A subscription event requires schema revisions")

    return {
        "event": {
            "commit": commit,
            "subscription_ids": list(message.subscription_ids),
            "query_events": [
                encode_query_event(event) for event in message.query_events
            ],
        }
    }


def decode_event_message(payload: object) -> EventMessage:
    """Validate the full notification before the client applies changes."""
    envelope = _object(payload, "event message")
    data = _object(envelope.get("event"), "subscription event")
    commit = decode_commit(data.get("commit"), require_stamp=True)

    if not commit.schema_revisions:
        raise ValueError("A subscription event requires schema revisions")

    subscription_ids = _ids(data.get("subscription_ids"), "subscription IDs")

    if not subscription_ids:
        raise ValueError("An event message requires at least one subscription")

    query_events = tuple(
        decode_query_event(value)
        for value in _list(data.get("query_events", []), "query events")
    )
    query_ids = tuple(event.subscription_id for event in query_events)

    if len(query_ids) != len(set(query_ids)):
        raise ValueError("An event message contains duplicate query events")

    if not set(query_ids).issubset(subscription_ids):
        raise ValueError("Query event IDs must belong to the event message")

    return EventMessage(
        commit=commit,
        subscription_ids=subscription_ids,
        query_events=query_events,
    )


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Serialized {name} must be an object")

    return value


def _list(value: object, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"Serialized {name} must be a list")

    return value


def _required(payload: dict[str, Any], key: str) -> Any:
    if key not in payload:
        raise ValueError(f"Store event must contain {key}")

    return payload[key]


def _string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)

    if not isinstance(value, str) or not value:
        raise ValueError(f"Store event {key} must be a non-empty string")

    return value


def _optional_string(payload: dict[str, Any], key: str) -> str | None:
    if payload.get(key) is None:
        return None

    return _string(payload, key)


def _schema_revisions(value: object) -> dict[str, int]:
    data = _object(value, "schema revisions")
    revisions: dict[str, int] = {}

    for schema, revision in data.items():
        if not isinstance(schema, str) or not schema:
            raise ValueError("Revision schema names must be non-empty strings")

        if type(revision) is not int or revision < 0:
            raise ValueError("Schema revisions must be non-negative integers")

        revisions[schema] = revision

    return revisions


def _validate_stamp(
    instance_id: str | None,
    revisions: dict[str, int],
    events: tuple[StoreEvent, ...],
    *,
    require_stamp: bool,
) -> None:
    if instance_id is None:
        if require_stamp or revisions:
            raise ValueError("Schema revisions require a Store instance ID")

        return

    if not isinstance(instance_id, str) or not instance_id:
        raise ValueError("Store instance ID must be a non-empty string")

    if any(event.schema not in revisions for event in events):
        raise ValueError("Every Store event schema requires a revision")


def _row(value: object) -> dict[str, Any]:
    data = _object(value, "Store row")

    if any(not isinstance(key, str) or not key for key in data):
        raise ValueError("Store row field names must be non-empty strings")

    return dict(data)


def _ids(value: object, name: str) -> tuple[str, ...]:
    values = _list(value, name)

    if any(not isinstance(item, str) or not item for item in values):
        raise ValueError(f"Serialized {name} must contain non-empty strings")

    if len(values) != len(set(values)):
        raise ValueError(f"Serialized {name} cannot contain duplicates")

    return tuple(values)
