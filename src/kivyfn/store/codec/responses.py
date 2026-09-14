# src/kivyfn/store/codec/responses.py
from __future__ import annotations

from typing import Any, assert_never

from kivyfn.store.codec.query import decode_query_rows, encode_query_rows
from kivyfn.store.error.codec import decode_failure, encode_failure
from kivyfn.store.protocol.message.responses import (
    CollectionSubscriptionResponse,
    FailureResponse,
    QueryResponse,
    QuerySubscriptionResponse,
    ResponseBatch,
    ResponseItem,
    SingleSubscriptionResponse,
    TransactionFailureResponse,
    TransactionResponse,
    UnsubscribeResponse,
)


def encode_response(response: ResponseBatch) -> dict[str, Any]:
    """Encode one ordered response batch."""
    return {"responses": [_encode_item(item) for item in response.items]}


def decode_response(payload: object) -> ResponseBatch:
    """Decode and validate one ordered response batch."""
    data = _object(payload, "response message")
    values = _list(data.get("responses"), "responses")

    if not values:
        raise ValueError("Store response batch cannot be empty")

    return ResponseBatch(items=tuple(_decode_item(value) for value in values))


def _encode_item(response: ResponseItem) -> dict[str, Any]:
    match response:
        case TransactionResponse():
            return {
                "type": "transaction",
                "transaction_id": response.transaction_id,
            }

        case TransactionFailureResponse():
            return {
                "type": "transaction_failure",
                "transaction_id": response.transaction_id,
                "failure": encode_failure(response.failure),
            }

        case QueryResponse():
            return {"type": "query", "rows": encode_query_rows(response.rows)}

        case SingleSubscriptionResponse():
            return {
                "type": "subscribe_single",
                "subscription_id": response.subscription_id,
                "store_instance_id": response.store_instance_id,
                "revision": response.revision,
                "values": (
                    None if response.values is None else dict(response.values)
                ),
            }

        case CollectionSubscriptionResponse():
            return {
                "type": "subscribe_collection",
                "subscription_id": response.subscription_id,
                "store_instance_id": response.store_instance_id,
                "revision": response.revision,
                "rows": (
                    None
                    if response.rows is None
                    else encode_query_rows(response.rows)
                ),
            }

        case QuerySubscriptionResponse():
            return {
                "type": "subscribe_query",
                "subscription_id": response.subscription_id,
                "store_instance_id": response.store_instance_id,
                "revisions": dict(response.revisions),
                "rows": (
                    None
                    if response.rows is None
                    else encode_query_rows(response.rows)
                ),
            }

        case UnsubscribeResponse():
            return {
                "type": "unsubscribe",
                "subscription_id": response.subscription_id,
            }

        case FailureResponse():
            return {
                "type": "failure",
                "failure": encode_failure(response.failure),
            }

        case _:
            assert_never(response)


def _decode_item(payload: object) -> ResponseItem:
    data = _object(payload, "response item")
    kind = _string(data, "type")

    match kind:
        case "transaction":
            return TransactionResponse(
                transaction_id=_string(data, "transaction_id")
            )

        case "transaction_failure":
            return TransactionFailureResponse(
                transaction_id=_string(data, "transaction_id"),
                failure=decode_failure(data.get("failure")),
            )

        case "query":
            return QueryResponse(rows=decode_query_rows(data.get("rows")))

        case "subscribe_single":
            return SingleSubscriptionResponse(
                subscription_id=_string(data, "subscription_id"),
                store_instance_id=_string(data, "store_instance_id"),
                revision=_revision(data),
                values=_snapshot_values(data),
            )

        case "subscribe_collection":
            return CollectionSubscriptionResponse(
                subscription_id=_string(data, "subscription_id"),
                store_instance_id=_string(data, "store_instance_id"),
                revision=_revision(data),
                rows=_snapshot_rows(data),
            )

        case "subscribe_query":
            return QuerySubscriptionResponse(
                subscription_id=_string(data, "subscription_id"),
                store_instance_id=_string(data, "store_instance_id"),
                revisions=_revisions(data.get("revisions")),
                rows=_snapshot_rows(data),
            )

        case "unsubscribe":
            return UnsubscribeResponse(
                subscription_id=_string(data, "subscription_id")
            )

        case "failure":
            return FailureResponse(failure=decode_failure(data.get("failure")))

        case _:
            raise ValueError(f"Unknown Store response type: {kind!r}")


def _snapshot_values(
    payload: dict[str, Any],
) -> tuple[tuple[str, Any], ...] | None:
    if "values" not in payload:
        raise ValueError("Subscription response must contain values")

    value = payload["values"]

    if value is None:
        return None

    values = _object(value, "snapshot values")

    if any(not isinstance(key, str) or not key for key in values):
        raise ValueError("Store field names must be non-empty strings")

    return tuple(values.items())


def _snapshot_rows(payload: dict[str, Any]):
    if "rows" not in payload:
        raise ValueError("Subscription response must contain rows")

    value = payload["rows"]

    if value is None:
        return None

    return decode_query_rows(value)


def _revisions(value: object) -> dict[str, int]:
    values = _object(value, "schema revisions")

    if not values:
        raise ValueError("Query revisions cannot be empty")

    revisions: dict[str, int] = {}

    for schema, revision in values.items():
        if not isinstance(schema, str) or not schema:
            raise ValueError("Revision schema names must be non-empty strings")

        revisions[schema] = _revision_value(revision)

    return revisions


def _revision(payload: dict[str, Any]) -> int:
    return _revision_value(payload.get("revision"))


def _revision_value(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("Schema revision must be a non-negative integer")

    return value


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
        raise ValueError(f"Store response {key} must be a non-empty string")

    return value
