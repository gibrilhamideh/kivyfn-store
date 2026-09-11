# src/kivyfn/store/codec/requests.py
from __future__ import annotations

from typing import Any, assert_never

from kivyfn.store.codec.query import query_from_payload, query_to_payload
from kivyfn.store.protocol.message.requests import (
    CollectionSubscriptionRequest,
    InsertRequest,
    MutationRequest,
    OperationRequest,
    QueryRequest,
    QuerySubscriptionRequest,
    RemoveRequest,
    Request,
    RequestType,
    SetRequest,
    SetRowRequest,
    SignalRequest,
    SignalRowRequest,
    SingleSubscriptionRequest,
    TransactionRequest,
    UnsubscribeRequest,
)


def encode_request(request: Request) -> dict[str, Any]:
    """Encode a typed request batch as {"requests": [...]}.

    Request and transaction mutation order are preserved. Field values and
    signal data retain their transport representation; the Store applies
    schema rules when processing the decoded request.
    """
    return {"requests": [_encode_item(item) for item in request.items]}


def decode_request(payload: object) -> Request:
    """Decode the whole batch before any request is dispatched.

    Malformed structure raises ValueError. Nested query decoding can also
    raise TypeError for invalid query values. Domain checks such as schema
    existence and field constraints belong to the Store.

    The returned items retain wire order. The host must process them in that
    order; a batch groups delivery and does not make its items atomic.
    """
    data = _object(payload, "request message")
    values = _list(data.get("requests"), "requests")

    if not values:
        raise ValueError("Store request batch cannot be empty")

    items = tuple(_decode_item(value) for value in values)
    request_ids = tuple(item.request_id for item in items)

    if len(request_ids) != len(set(request_ids)):
        raise ValueError("Store request batch contains duplicate request IDs")

    return Request(items=items)


def _encode_item(request: RequestType) -> dict[str, Any]:
    payload: dict[str, Any] = {"request_id": request.request_id}

    match request:
        case OperationRequest():
            payload.update(
                type="operation",
                operation_id=request.operation_id,
                mutation=_encode_mutation(request.mutation),
            )
        case TransactionRequest():
            payload.update(
                type="transaction",
                operation_id=request.operation_id,
                mutations=[
                    _encode_mutation(mutation)
                    for mutation in request.mutations
                ],
            )
        case QueryRequest():
            payload.update(type="query", query=query_to_payload(request.query))
        case SingleSubscriptionRequest():
            payload.update(type="subscribe_single", schema=request.schema)
        case CollectionSubscriptionRequest():
            payload.update(type="subscribe_collection", schema=request.schema)
        case QuerySubscriptionRequest():
            payload.update(
                type="subscribe_query", query=query_to_payload(request.query)
            )
        case UnsubscribeRequest():
            payload.update(
                type="unsubscribe", subscription_id=request.subscription_id
            )
        case _:
            assert_never(request)

    return payload


def _decode_item(payload: object) -> RequestType:
    data = _object(payload, "request item")
    kind = _string(data, "type")
    request_id = _string(data, "request_id")

    match kind:
        case "operation":
            return OperationRequest(
                request_id=request_id,
                operation_id=_string(data, "operation_id"),
                mutation=_decode_mutation(data.get("mutation")),
            )
        case "transaction":
            mutations = _list(data.get("mutations"), "transaction mutations")

            if not mutations:
                raise ValueError("Store transaction cannot be empty")

            return TransactionRequest(
                request_id=request_id,
                operation_id=_string(data, "operation_id"),
                mutations=tuple(
                    _decode_mutation(value) for value in mutations
                ),
            )
        case "query":
            return QueryRequest(
                request_id=request_id,
                query=query_from_payload(data.get("query")),
            )
        case "subscribe_single":
            return SingleSubscriptionRequest(
                request_id=request_id, schema=_string(data, "schema")
            )
        case "subscribe_collection":
            return CollectionSubscriptionRequest(
                request_id=request_id, schema=_string(data, "schema")
            )
        case "subscribe_query":
            return QuerySubscriptionRequest(
                request_id=request_id,
                query=query_from_payload(data.get("query")),
            )
        case "unsubscribe":
            return UnsubscribeRequest(
                request_id=request_id,
                subscription_id=_string(data, "subscription_id"),
            )
        case _:
            raise ValueError(f"Unknown Store request type: {kind!r}")


def _encode_mutation(mutation: MutationRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {"schema": mutation.schema}

    match mutation:
        case SetRequest():
            payload.update(type="set", values=dict(mutation.values))
        case SetRowRequest():
            payload.update(
                type="setrow",
                row_id=mutation.row_id,
                values=dict(mutation.values),
            )
        case InsertRequest():
            payload.update(
                type="insert",
                row_id=mutation.row_id,
                values=dict(mutation.values),
            )
        case RemoveRequest():
            payload.update(type="remove", row_id=mutation.row_id)
        case SignalRequest():
            payload.update(
                type="signal", signal=mutation.signal, data=mutation.data
            )
        case SignalRowRequest():
            payload.update(
                type="signalrow",
                row_id=mutation.row_id,
                signal=mutation.signal,
                data=mutation.data,
            )
        case _:
            assert_never(mutation)

    return payload


def _decode_mutation(payload: object) -> MutationRequest:
    data = _object(payload, "mutation")
    kind = _string(data, "type")
    schema = _string(data, "schema")

    match kind:
        case "set":
            return SetRequest(schema=schema, values=_values(data))
        case "setrow":
            return SetRowRequest(
                schema=schema,
                row_id=_string(data, "row_id"),
                values=_values(data),
            )
        case "insert":
            return InsertRequest(
                schema=schema,
                row_id=_string(data, "row_id"),
                values=_values(data, allow_empty=True),
            )
        case "remove":
            return RemoveRequest(schema=schema, row_id=_string(data, "row_id"))
        case "signal":
            return SignalRequest(
                schema=schema,
                signal=_string(data, "signal"),
                data=data.get("data"),
            )
        case "signalrow":
            return SignalRowRequest(
                schema=schema,
                row_id=_string(data, "row_id"),
                signal=_string(data, "signal"),
                data=data.get("data"),
            )
        case _:
            raise ValueError(f"Unknown Store mutation type: {kind!r}")


def _values(
    payload: dict[str, Any], *, allow_empty: bool = False
) -> tuple[tuple[str, Any], ...]:
    values = _object(payload.get("values"), "mutation values")

    if not values and not allow_empty:
        raise ValueError("Store mutation values cannot be empty")

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
