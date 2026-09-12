# src/kivyfn/store/codec/responses.py
from __future__ import annotations

from typing import TYPE_CHECKING, Any, assert_never

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


if TYPE_CHECKING:
    from kivyfn.store.query import QueryResult
    from kivyfn.store.query.shape import QueryShape


def encode_response(response: Response) -> dict[str, Any]:
    """Encode a typed response batch as {"responses": [...]} in order."""
    return {"responses": [_encode_item(item) for item in response.items]}


def decode_response(payload: object) -> Response:
    """Decode the complete batch before the client settles requests.

    Writes carry confirmations and schema revisions, never canonical values
    or commits. An explicit null subscription snapshot means unchanged;
    missing snapshot fields are malformed. Empty snapshots remain distinct.
    Correlation and cache validation belong to the client.
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
                store_instance_id=response.store_instance_id,
                schema_revisions=_schema_revisions(response.schema_revisions),
            )
        case SetRowResponse():
            payload.update(
                type="setrow",
                operation_id=response.operation_id,
                store_instance_id=response.store_instance_id,
                schema_revisions=_schema_revisions(response.schema_revisions),
                row_id=response.row_id,
            )
        case InsertResponse():
            payload.update(
                type="insert",
                operation_id=response.operation_id,
                store_instance_id=response.store_instance_id,
                schema_revisions=_schema_revisions(response.schema_revisions),
                row_id=response.row_id,
            )
        case RemoveResponse():
            payload.update(
                type="remove",
                operation_id=response.operation_id,
                store_instance_id=response.store_instance_id,
                schema_revisions=_schema_revisions(response.schema_revisions),
                row_id=response.row_id,
            )
        case SignalResponse():
            payload.update(
                type="signal",
                operation_id=response.operation_id,
                store_instance_id=response.store_instance_id,
            )
        case TransactionResponse():
            payload.update(
                type="transaction",
                operation_id=response.operation_id,
                store_instance_id=response.store_instance_id,
                schema_revisions=_schema_revisions(
                    response.schema_revisions, allow_empty=True
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
                values=(
                    None if response.values is None else dict(response.values)
                ),
            )
        case CollectionSubscriptionResponse():
            payload.update(
                type="subscribe_collection",
                subscription_id=response.subscription_id,
                store_instance_id=response.store_instance_id,
                revision=response.revision,
                rows=_encode_snapshot_rows(response.rows),
            )
        case QuerySubscriptionResponse():
            payload.update(
                type="subscribe_query",
                subscription_id=response.subscription_id,
                store_instance_id=response.store_instance_id,
                schema_revisions=_query_revisions(
                    response.schema_revisions, response.shape
                ),
                shape=encode_query_shape(response.shape),
                rows=_encode_snapshot_rows(response.rows),
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

    if kind not in ("subscribe_single", "subscribe_collection"):
        if "revision" in data:
            raise ValueError("Global response revisions are not supported")

    if kind in ("set", "setrow", "insert", "remove", "signal", "transaction"):
        if "values" in data or "commit" in data:
            raise ValueError("Write responses must not contain canonical data")

    if kind == "signal" and "schema_revisions" in data:
        raise ValueError("Signal responses do not carry schema revisions")

    match kind:
        case "set":
            return SetResponse(
                request_id=request_id,
                operation_id=_string(data, "operation_id"),
                store_instance_id=_string(data, "store_instance_id"),
                schema_revisions=_schema_revisions(
                    data.get("schema_revisions")
                ),
            )
        case "setrow":
            return SetRowResponse(
                request_id=request_id,
                operation_id=_string(data, "operation_id"),
                store_instance_id=_string(data, "store_instance_id"),
                schema_revisions=_schema_revisions(
                    data.get("schema_revisions")
                ),
                row_id=_string(data, "row_id"),
            )
        case "insert":
            return InsertResponse(
                request_id=request_id,
                operation_id=_string(data, "operation_id"),
                store_instance_id=_string(data, "store_instance_id"),
                schema_revisions=_schema_revisions(
                    data.get("schema_revisions")
                ),
                row_id=_string(data, "row_id"),
            )
        case "remove":
            return RemoveResponse(
                request_id=request_id,
                operation_id=_string(data, "operation_id"),
                store_instance_id=_string(data, "store_instance_id"),
                schema_revisions=_schema_revisions(
                    data.get("schema_revisions")
                ),
                row_id=_string(data, "row_id"),
            )
        case "signal":
            return SignalResponse(
                request_id=request_id,
                operation_id=_string(data, "operation_id"),
                store_instance_id=_string(data, "store_instance_id"),
            )
        case "transaction":
            return TransactionResponse(
                request_id=request_id,
                operation_id=_string(data, "operation_id"),
                store_instance_id=_string(data, "store_instance_id"),
                schema_revisions=_schema_revisions(
                    data.get("schema_revisions"), allow_empty=True
                ),
            )
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
                values=_snapshot_values(data),
            )
        case "subscribe_collection":
            return CollectionSubscriptionResponse(
                request_id=request_id,
                subscription_id=_string(data, "subscription_id"),
                store_instance_id=_string(data, "store_instance_id"),
                revision=_revision(data),
                rows=_snapshot_rows(data),
            )
        case "subscribe_query":
            shape = decode_query_shape(data.get("shape"))
            return QuerySubscriptionResponse(
                request_id=request_id,
                subscription_id=_string(data, "subscription_id"),
                store_instance_id=_string(data, "store_instance_id"),
                schema_revisions=_query_revisions(
                    data.get("schema_revisions"), shape
                ),
                shape=shape,
                rows=_snapshot_rows(data),
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
        raise ValueError("Schema revision must be a non-negative integer")

    return value


def _schema_revisions(
    value: object, *, allow_empty: bool = False
) -> dict[str, int]:
    data = _object(value, "schema revisions")

    if not data and not allow_empty:
        raise ValueError("Schema revisions must not be empty")

    revisions: dict[str, int] = {}

    for schema, revision in data.items():
        if not isinstance(schema, str) or not schema:
            raise ValueError("Revision schema names must be non-empty strings")

        if type(revision) is not int or revision < 0:
            raise ValueError("Schema revisions must be non-negative integers")

        revisions[schema] = revision

    return revisions


def _query_revisions(value: object, shape: QueryShape) -> dict[str, int]:
    revisions = _schema_revisions(value)
    dependencies = {
        shape.root_schema,
        *(join.target_schema for join in shape.joins),
    }

    if set(revisions) != dependencies:
        raise ValueError("Query revisions must match its dependency schemas")

    return revisions


def _required(payload: dict[str, Any], key: str) -> Any:
    if key not in payload:
        raise ValueError(f"Store response must contain {key}")

    return payload[key]


def _snapshot_values(
    payload: dict[str, Any],
) -> tuple[tuple[str, Any], ...] | None:
    value = _required(payload, "values")

    if value is None:
        return None

    values = _object(value, "snapshot values")

    if any(not isinstance(key, str) or not key for key in values):
        raise ValueError("Store field names must be non-empty strings")

    return tuple(values.items())


def _snapshot_rows(payload: dict[str, Any]) -> QueryResult | None:
    value = _required(payload, "rows")
    return None if value is None else decode_query_rows(value)


def _encode_snapshot_rows(
    rows: QueryResult | None,
) -> list[dict[str, Any]] | None:
    return None if rows is None else encode_query_rows(rows)
