# src/kivyfn/store/codec/responses.py
from __future__ import annotations

from typing import Any, assert_never

from kivyfn.store.codec.events import decode_commit, encode_commit
from kivyfn.store.codec.query import (
    decode_query_rows,
    decode_query_shape,
    encode_query_rows,
    encode_query_shape,
)
from kivyfn.store.error.codec import decode_failure, encode_failure
from kivyfn.store.protocol.message.responses import (
    CollectionSubscriptionResponse,
    FailureResponse,
    InsertResponse,
    QueryResponse,
    QuerySubscriptionResponse,
    RemoveResponse,
    Response,
    ResponseValue,
    SetResponse,
    SetRowResponse,
    SignalResponse,
    SingleSubscriptionResponse,
    TransactionResponse,
    UnsubscribeResponse,
)


def encode_response(response: Response) -> dict[str, Any]:
    """Encode a typed response batch as {"responses": [...]} in order."""
    return {"responses": [_encode_item(item) for item in response.items]}


def decode_response(payload: object) -> Response:
    """Decode the complete batch before the client settles requests.

    Results, commits, failures, rows, and query shapes become their shared
    Python types. Correlation with pending requests belongs to the client.
    Malformed payloads raise ValueError or a nested codec's TypeError.
    """
    data = _object(payload, "response message")
    values = data.get("responses")

    if not isinstance(values, list):
        raise ValueError("Serialized responses must be a list")

    if not values:
        raise ValueError("Store response batch cannot be empty")

    items = tuple(_decode_item(value) for value in values)
    request_ids = tuple(item.request_id for item in items)

    if len(request_ids) != len(set(request_ids)):
        raise ValueError("Store response batch contains duplicate request IDs")

    return Response(items=items)


def _encode_item(response: ResponseValue) -> dict[str, Any]:
    payload: dict[str, Any] = {"request_id": response.request_id}

    match response:
        case SetResponse():
            payload.update(
                type="set",
                operation_id=response.operation_id,
                revision=response.revision,
                values=dict(response.values),
            )
        case SetRowResponse():
            payload.update(
                type="setrow",
                operation_id=response.operation_id,
                revision=response.revision,
                row_id=response.row_id,
                values=dict(response.values),
            )
        case InsertResponse():
            payload.update(
                type="insert",
                operation_id=response.operation_id,
                revision=response.revision,
                row_id=response.row_id,
                values=dict(response.values),
            )
        case RemoveResponse():
            payload.update(
                type="remove",
                operation_id=response.operation_id,
                revision=response.revision,
                row_id=response.row_id,
            )
        case SignalResponse():
            payload.update(
                type="signal",
                operation_id=response.operation_id,
                revision=response.revision,
            )
        case TransactionResponse():
            payload.update(
                type="transaction",
                operation_id=response.operation_id,
                revision=response.revision,
                commit=(
                    encode_commit(response.commit)
                    if response.commit is not None
                    else None
                ),
            )
        case QueryResponse():
            payload.update(type="query", rows=encode_query_rows(response.rows))
        case SingleSubscriptionResponse():
            payload.update(
                type="subscribe_single",
                subscription_id=response.subscription_id,
                store_instance_id=response.store_instance_id,
                revision=response.revision,
                values=dict(response.values),
            )
        case CollectionSubscriptionResponse():
            payload.update(
                type="subscribe_collection",
                subscription_id=response.subscription_id,
                store_instance_id=response.store_instance_id,
                revision=response.revision,
                rows=encode_query_rows(response.rows),
            )
        case QuerySubscriptionResponse():
            payload.update(
                type="subscribe_query",
                subscription_id=response.subscription_id,
                store_instance_id=response.store_instance_id,
                revision=response.revision,
                shape=encode_query_shape(response.shape),
                rows=encode_query_rows(response.rows),
            )
        case UnsubscribeResponse():
            payload.update(
                type="unsubscribe", subscription_id=response.subscription_id
            )
        case FailureResponse():
            payload.update(
                type="failure", failure=encode_failure(response.failure)
            )
        case _:
            assert_never(response)

    return payload


def _decode_item(payload: object) -> ResponseValue:
    data = _object(payload, "response item")
    kind = _string(data, "type")
    request_id = _string(data, "request_id")

    match kind:
        case "set":
            return SetResponse(
                request_id=request_id,
                operation_id=_string(data, "operation_id"),
                revision=_revision(data),
                values=_values(data),
            )
        case "setrow":
            return SetRowResponse(
                request_id=request_id,
                operation_id=_string(data, "operation_id"),
                revision=_revision(data),
                row_id=_string(data, "row_id"),
                values=_values(data),
            )
        case "insert":
            return InsertResponse(
                request_id=request_id,
                operation_id=_string(data, "operation_id"),
                revision=_revision(data),
                row_id=_string(data, "row_id"),
                values=_values(data, allow_empty=True),
            )
        case "remove":
            return RemoveResponse(
                request_id=request_id,
                operation_id=_string(data, "operation_id"),
                revision=_revision(data),
                row_id=_string(data, "row_id"),
            )
        case "signal":
            return SignalResponse(
                request_id=request_id,
                operation_id=_string(data, "operation_id"),
                revision=_revision(data),
            )
        case "transaction":
            return _decode_transaction(data, request_id)
        case "query":
            return QueryResponse(
                request_id=request_id, rows=decode_query_rows(data.get("rows"))
            )
        case "subscribe_single":
            return SingleSubscriptionResponse(
                request_id=request_id,
                subscription_id=_string(data, "subscription_id"),
                store_instance_id=_string(data, "store_instance_id"),
                revision=_revision(data),
                values=_values(data, allow_empty=True),
            )
        case "subscribe_collection":
            return CollectionSubscriptionResponse(
                request_id=request_id,
                subscription_id=_string(data, "subscription_id"),
                store_instance_id=_string(data, "store_instance_id"),
                revision=_revision(data),
                rows=decode_query_rows(data.get("rows")),
            )
        case "subscribe_query":
            return QuerySubscriptionResponse(
                request_id=request_id,
                subscription_id=_string(data, "subscription_id"),
                store_instance_id=_string(data, "store_instance_id"),
                revision=_revision(data),
                shape=decode_query_shape(data.get("shape")),
                rows=decode_query_rows(data.get("rows")),
            )
        case "unsubscribe":
            return UnsubscribeResponse(
                request_id=request_id,
                subscription_id=_string(data, "subscription_id"),
            )
        case "failure":
            return FailureResponse(
                request_id=request_id,
                failure=decode_failure(data.get("failure")),
            )
        case _:
            raise ValueError(f"Unknown Store response type: {kind!r}")


def _decode_transaction(
    data: dict[str, Any], request_id: str
) -> TransactionResponse:
    operation_id = _string(data, "operation_id")
    revision = _revision(data)
    payload = data.get("commit")
    commit = (
        decode_commit(payload, require_stamp=True)
        if payload is not None
        else None
    )

    if commit is not None:
        if commit.operation_id != operation_id or commit.revision != revision:
            raise ValueError("Transaction response and commit metadata differ")

    return TransactionResponse(
        request_id=request_id,
        operation_id=operation_id,
        revision=revision,
        commit=commit,
    )


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Serialized {name} must be an object")

    return value


def _string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)

    if not isinstance(value, str) or not value:
        raise ValueError(f"Store response {key} must be a non-empty string")

    return value


def _revision(payload: dict[str, Any]) -> int:
    value = payload.get("revision")

    if type(value) is not int or value < 0:
        raise ValueError("Store revision must be a non-negative integer")

    return value


def _values(
    payload: dict[str, Any], *, allow_empty: bool = False
) -> tuple[tuple[str, Any], ...]:
    values = _object(payload.get("values"), "response values")

    if not values and not allow_empty:
        raise ValueError("Store response values cannot be empty")

    if any(not isinstance(key, str) or not key for key in values):
        raise ValueError("Store field names must be non-empty strings")

    return tuple(values.items())
