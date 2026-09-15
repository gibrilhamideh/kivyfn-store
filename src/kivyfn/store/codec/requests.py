# src/kivyfn/store/codec/requests.py
from __future__ import annotations

from typing import Any, assert_never

from kivyfn.store.codec.history import decode_position, encode_position
from kivyfn.store.codec.query import query_from_payload, query_to_payload
from kivyfn.store.protocol.message.requests import (
    CollectionSubscriptionRequest,
    HistoryRequest,
    InsertRequestAction,
    QueryRequest,
    QuerySubscriptionRequest,
    RedoRequest,
    RemoveRequestAction,
    RequestAction,
    RequestBatch,
    RequestItem,
    SetRequestAction,
    SetRowRequestAction,
    SignalRequestAction,
    SignalRowRequestAction,
    SingleSubscriptionRequest,
    TransactionRequest,
    UndoRequest,
    UnsubscribeRequest,
)


def encode_request(request: RequestBatch) -> dict[str, Any]:
    """Encode one ordered request batch."""
    return {"requests": [_encode_item(item) for item in request.items]}


def decode_request(payload: object) -> RequestBatch:
    """Decode and validate one ordered request batch."""
    data = _object(payload, "request message")
    values = _list(data.get("requests"), "requests")

    if not values:
        raise ValueError("Store request batch cannot be empty")

    return RequestBatch(items=tuple(_decode_item(value) for value in values))


def _encode_item(request: RequestItem) -> dict[str, Any]:
    match request:
        case TransactionRequest():
            payload: dict[str, Any] = {
                "type": "transaction",
                "transaction_id": request.transaction_id,
                "actions": [
                    _encode_action(action) for action in request.actions
                ],
            }
            for name in (
                "history_group",
                "history_label",
                "expected_store_instance_id",
                "expected_revisions",
            ):
                value = getattr(request, name)
                if value is not None:
                    payload[name] = value
            return payload

        case HistoryRequest():
            return {
                "type": "history",
                "limit": request.limit,
                "offset": request.offset,
            }

        case UndoRequest() | RedoRequest():
            return {
                "type": "undo" if isinstance(request, UndoRequest) else "redo",
                "transaction_id": request.transaction_id,
                "expected_position": encode_position(
                    request.expected_position
                ),
            }

        case QueryRequest():
            return {"type": "query", "query": query_to_payload(request.query)}

        case SingleSubscriptionRequest():
            payload = {"type": "subscribe_single", "schema": request.schema}

            _encode_schema_cache(
                payload, request.store_instance_id, request.revision
            )

            return payload

        case CollectionSubscriptionRequest():
            payload = {
                "type": "subscribe_collection",
                "schema": request.schema,
            }

            _encode_schema_cache(
                payload, request.store_instance_id, request.revision
            )

            return payload

        case QuerySubscriptionRequest():
            payload = {
                "type": "subscribe_query",
                "query": query_to_payload(request.query),
            }

            _encode_query_cache(
                payload, request.store_instance_id, request.revisions
            )

            return payload

        case UnsubscribeRequest():
            return {
                "type": "unsubscribe",
                "subscription_id": request.subscription_id,
            }

        case _:
            assert_never(request)


def _decode_item(payload: object) -> RequestItem:
    data = _object(payload, "request item")
    kind = _string(data, "type")

    match kind:
        case "transaction":
            actions = _list(data.get("actions"), "transaction actions")

            return TransactionRequest(
                transaction_id=_string(data, "transaction_id"),
                actions=tuple(_decode_action(action) for action in actions),
                history_group=_optional_string(data, "history_group"),
                history_label=_optional_string(data, "history_label"),
                expected_store_instance_id=_optional_string(
                    data, "expected_store_instance_id"
                ),
                expected_revisions=_edit_revisions(data),
            )

        case "history":
            limit = _revision(data.get("limit", 100))
            if not 1 <= limit <= 1000:
                raise ValueError("History limit must be between 1 and 1000")
            return HistoryRequest(limit, _revision(data.get("offset", 0)))

        case "undo" | "redo":
            request_type = UndoRequest if kind == "undo" else RedoRequest
            return request_type(
                transaction_id=_string(data, "transaction_id"),
                expected_position=decode_position(
                    data.get("expected_position")
                ),
            )

        case "query":
            return QueryRequest(query=query_from_payload(data.get("query")))

        case "subscribe_single":
            instance_id, revision = _schema_cache(
                data.get("store_instance_id"), data.get("revision")
            )

            return SingleSubscriptionRequest(
                schema=_string(data, "schema"),
                store_instance_id=instance_id,
                revision=revision,
            )

        case "subscribe_collection":
            instance_id, revision = _schema_cache(
                data.get("store_instance_id"), data.get("revision")
            )

            return CollectionSubscriptionRequest(
                schema=_string(data, "schema"),
                store_instance_id=instance_id,
                revision=revision,
            )

        case "subscribe_query":
            instance_id, revisions = _query_cache(
                data.get("store_instance_id"), data.get("revisions")
            )

            return QuerySubscriptionRequest(
                query=query_from_payload(data.get("query")),
                store_instance_id=instance_id,
                revisions=revisions,
            )

        case "unsubscribe":
            return UnsubscribeRequest(
                subscription_id=_string(data, "subscription_id")
            )

        case _:
            raise ValueError(f"Unknown Store request type: {kind!r}")


def _encode_action(action: RequestAction) -> dict[str, Any]:
    match action:
        case SetRequestAction():
            return {
                "type": "set",
                "schema": action.schema,
                "values": dict(action.values),
            }

        case SetRowRequestAction():
            return {
                "type": "setrow",
                "schema": action.schema,
                "row_id": action.row_id,
                "values": dict(action.values),
            }

        case InsertRequestAction():
            return {
                "type": "insert",
                "schema": action.schema,
                "row_id": action.row_id,
                "values": dict(action.values),
            }

        case RemoveRequestAction():
            return {
                "type": "remove",
                "schema": action.schema,
                "row_id": action.row_id,
            }

        case SignalRequestAction():
            return {
                "type": "signal",
                "schema": action.schema,
                "signal": action.signal,
                "data": action.data,
            }

        case SignalRowRequestAction():
            return {
                "type": "signalrow",
                "schema": action.schema,
                "row_id": action.row_id,
                "signal": action.signal,
                "data": action.data,
            }

        case _:
            assert_never(action)


def _decode_action(payload: object) -> RequestAction:
    data = _object(payload, "request action")
    kind = _string(data, "type")
    schema = _string(data, "schema")

    match kind:
        case "set":
            return SetRequestAction(schema=schema, values=_values(data))

        case "setrow":
            return SetRowRequestAction(
                schema=schema,
                row_id=_string(data, "row_id"),
                values=_values(data),
            )

        case "insert":
            return InsertRequestAction(
                schema=schema,
                row_id=_string(data, "row_id"),
                values=_values(data),
            )

        case "remove":
            return RemoveRequestAction(
                schema=schema, row_id=_string(data, "row_id")
            )

        case "signal":
            return SignalRequestAction(
                schema=schema,
                signal=_string(data, "signal"),
                data=data.get("data"),
            )

        case "signalrow":
            return SignalRowRequestAction(
                schema=schema,
                row_id=_string(data, "row_id"),
                signal=_string(data, "signal"),
                data=data.get("data"),
            )

        case _:
            raise ValueError(f"Unknown Store request action: {kind!r}")


def _encode_schema_cache(
    payload: dict[str, Any], instance_id: str | None, revision: int | None
) -> None:
    if instance_id is None and revision is None:
        return

    if instance_id is None or revision is None:
        raise ValueError(
            "Schema subscription cache requires "
            "Store instance and revision together"
        )

    payload["store_instance_id"] = instance_id
    payload["revision"] = revision


def _encode_query_cache(
    payload: dict[str, Any],
    instance_id: str | None,
    revisions: dict[str, int] | None,
) -> None:
    if instance_id is None and revisions is None:
        return

    if instance_id is None or revisions is None:
        raise ValueError(
            "Query subscription cache requires "
            "Store instance and revisions together"
        )

    payload["store_instance_id"] = instance_id
    payload["revisions"] = dict(revisions)


def _schema_cache(
    instance_id: object, revision: object
) -> tuple[str | None, int | None]:
    if instance_id is None and revision is None:
        return None, None

    instance = _cache_instance(instance_id, revision)

    return instance, _revision(revision)


def _query_cache(
    instance_id: object, revisions: object
) -> tuple[str | None, dict[str, int] | None]:
    if instance_id is None and revisions is None:
        return None, None

    instance = _cache_instance(instance_id, revisions)

    values = _object(revisions, "cached revisions")

    if not values:
        raise ValueError("Cached query revisions cannot be empty")

    result: dict[str, int] = {}

    for schema, revision in values.items():
        if not isinstance(schema, str) or not schema:
            raise ValueError("Cached schema names must be non-empty strings")

        result[schema] = _revision(revision)

    return instance, result


def _cache_instance(instance_id: object, revisions: object) -> str:
    if (
        not isinstance(instance_id, str)
        or not instance_id
        or revisions is None
    ):
        raise ValueError(
            "Subscription cache requires a non-empty "
            "store_instance_id and revisions together"
        )

    return instance_id


def _revision(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("Cached revision must be a non-negative integer")

    return value


def _values(payload: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
    # The Store owns action semantics; the codec validates their wire shape.
    values = _object(payload.get("values"), "action values")

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
        raise ValueError(f"Store request {key} must be a non-empty string")

    return value


def _optional_string(payload: dict[str, Any], key: str) -> str | None:
    return None if payload.get(key) is None else _string(payload, key)


def _edit_revisions(payload: dict[str, Any]) -> dict[str, int] | None:
    value = payload.get("expected_revisions")
    if value is None:
        return None
    values = _object(value, "expected revisions")
    result: dict[str, int] = {}
    for schema, revision in values.items():
        if not isinstance(schema, str) or not schema:
            raise ValueError("Revision schema names must be non-empty strings")
        result[schema] = _revision(revision)
    return result
