# src/kivyfn/store/codec/events.py
from __future__ import annotations

from typing import Any, assert_never

from kivyfn.store.protocol.message.event import (
    EventAction,
    EventMessage,
    InsertEventAction,
    RemoveEventAction,
    SetEventAction,
    SetRowEventAction,
    SignalEventAction,
    SignalRowEventAction,
    SubscriptionEvent,
)


def encode_event_message(message: EventMessage) -> dict[str, Any]:
    """Encode one Store subscription event message."""
    return {
        "event": {
            "store_instance_id": message.store_instance_id,
            "transaction_id": message.transaction_id,
            "events": [
                _encode_subscription_event(event) for event in message.events
            ],
        }
    }


def decode_event_message(payload: object) -> EventMessage:
    """Decode and validate one Store subscription event message."""
    envelope = _object(payload, "event message")
    data = _object(envelope.get("event"), "event")
    events = _list(data.get("events"), "subscription events")

    if not events:
        raise ValueError("Store event message cannot be empty")

    return EventMessage(
        store_instance_id=_string(data, "store_instance_id"),
        transaction_id=_optional_string(data, "transaction_id"),
        events=tuple(_decode_subscription_event(event) for event in events),
    )


def _encode_subscription_event(event: SubscriptionEvent) -> dict[str, Any]:
    payload = {
        "subscription_id": event.subscription_id,
        "revisions": dict(event.revisions),
        "actions": [_encode_action(action) for action in event.actions],
    }
    return payload


def _decode_subscription_event(payload: object) -> SubscriptionEvent:
    data = _object(payload, "subscription event")

    return SubscriptionEvent(
        subscription_id=_string(data, "subscription_id"),
        revisions=_revisions(data.get("revisions")),
        actions=tuple(
            _decode_action(action)
            for action in _list(data.get("actions"), "event actions")
        ),
    )


def _encode_action(action: EventAction) -> dict[str, Any]:
    match action:
        case SetEventAction():
            return {"type": "set", "values": dict(action.values)}

        case SetRowEventAction():
            return {
                "type": "setrow",
                "row_id": action.row_id,
                "values": dict(action.values),
            }

        case InsertEventAction():
            return {
                "type": "insert",
                "row_id": action.row_id,
                "values": dict(action.values),
            }

        case RemoveEventAction():
            return {"type": "remove", "row_id": action.row_id}

        case SignalEventAction():
            return {
                "type": "signal",
                "signal": action.signal,
                "data": action.data,
                "occurrence_id": action.occurrence_id,
            }

        case SignalRowEventAction():
            return {
                "type": "signalrow",
                "row_id": action.row_id,
                "signal": action.signal,
                "data": action.data,
                "occurrence_id": action.occurrence_id,
            }

        case _:
            assert_never(action)


def _decode_action(payload: object) -> EventAction:
    data = _object(payload, "event action")
    kind = _string(data, "type")

    match kind:
        case "set":
            return SetEventAction(values=_values(data))

        case "setrow":
            return SetRowEventAction(
                row_id=_string(data, "row_id"), values=_values(data)
            )

        case "insert":
            return InsertEventAction(
                row_id=_string(data, "row_id"),
                values=_values(data, allow_empty=True),
            )

        case "remove":
            return RemoveEventAction(row_id=_string(data, "row_id"))

        case "signal":
            return SignalEventAction(
                signal=_string(data, "signal"),
                data=data.get("data"),
                occurrence_id=_occurrence_id(data),
            )

        case "signalrow":
            return SignalRowEventAction(
                row_id=_string(data, "row_id"),
                signal=_string(data, "signal"),
                data=data.get("data"),
                occurrence_id=_occurrence_id(data),
            )

        case _:
            raise ValueError(f"Unknown Store event action: {kind!r}")


def _revisions(value: object) -> dict[str, int]:
    values = _object(value, "subscription revisions")
    revisions: dict[str, int] = {}

    for schema, revision in values.items():
        if not isinstance(schema, str) or not schema:
            raise ValueError("Revision schema names must be non-empty strings")

        if type(revision) is not int or revision < 0:
            raise ValueError(
                "Subscription revisions must be non-negative integers"
            )

        revisions[schema] = revision

    return revisions


def _values(
    payload: dict[str, Any], *, allow_empty: bool = False
) -> tuple[tuple[str, Any], ...]:
    values = _object(payload.get("values"), "event values")

    if not values and not allow_empty:
        raise ValueError("Store event values cannot be empty")

    if any(not isinstance(key, str) or not key for key in values):
        raise ValueError("Store field names must be non-empty strings")

    return tuple(values.items())


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Serialized {name} must be an object")

    return value


def _list(value: object, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"Serialized {name} must be a list")

    return value


def _string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)

    if not isinstance(value, str) or not value:
        raise ValueError(f"Store event {key} must be a non-empty string")

    return value


def _optional_string(payload: dict[str, Any], key: str) -> str | None:
    if payload.get(key) is None:
        return None
    return _string(payload, key)


def _occurrence_id(payload: dict[str, Any]) -> int | None:
    value = payload.get("occurrence_id")
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError("Signal occurrence ID must be a nonnegative integer")
    return value
