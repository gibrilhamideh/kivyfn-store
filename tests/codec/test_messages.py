from __future__ import annotations

import json
from typing import Any

import pytest

from kivyfn.store.codec.events import (
    decode_event_message,
    encode_event_message,
)
from kivyfn.store.codec.requests import decode_request, encode_request
from kivyfn.store.codec.responses import decode_response, encode_response
from kivyfn.store.error import (
    StoreErrorCategory,
    StoreErrorCode,
    StoreErrorStatus,
    StoreFailure,
)
from kivyfn.store.protocol.message.event import (
    EventMessage,
    InsertEventAction,
    RemoveEventAction,
    SetEventAction,
    SetRowEventAction,
    SignalEventAction,
    SignalRowEventAction,
    SubscriptionEvent,
)
from kivyfn.store.protocol.message.requests import (
    CollectionSubscriptionRequest,
    InsertRequestAction,
    QueryRequest,
    QuerySubscriptionRequest,
    RemoveRequestAction,
    RequestBatch,
    SetRequestAction,
    SetRowRequestAction,
    SignalRequestAction,
    SignalRowRequestAction,
    SingleSubscriptionRequest,
    TransactionRequest,
    UnsubscribeRequest,
)
from kivyfn.store.protocol.message.responses import (
    CollectionSubscriptionResponse,
    FailureResponse,
    QueryResponse,
    QuerySubscriptionResponse,
    ResponseBatch,
    SingleSubscriptionResponse,
    TransactionFailureResponse,
    TransactionResponse,
    UnsubscribeResponse,
)
from kivyfn.store.query import Query, QueryRow


def test_all_request_kinds_survive_json_wire_roundtrip() -> None:
    actions = (
        SetRequestAction("Single", (("value", 1),)),
        SetRowRequestAction("Many", "row", (("value", False),)),
        InsertRequestAction("Many", "empty", ()),
        RemoveRequestAction("Many", "row"),
        SignalRequestAction("Single", "ping"),
        SignalRowRequestAction("Many", "row", "ping", [1, {"x": None}]),
    )
    batch = RequestBatch(
        (
            TransactionRequest("transaction", actions),
            QueryRequest(Query("Many")),
            SingleSubscriptionRequest("Single"),
            SingleSubscriptionRequest("Single", "instance", 0),
            CollectionSubscriptionRequest("Many", "instance", 4),
            QuerySubscriptionRequest(Query("Many"), "instance", {"Many": 4}),
            UnsubscribeRequest("subscription"),
        )
    )
    assert (
        decode_request(json.loads(json.dumps(encode_request(batch)))) == batch
    )


def test_all_response_kinds_and_empty_vs_cached_snapshots() -> None:
    failure = StoreFailure(
        code=StoreErrorCode.INTERNAL_ERROR,
        category=StoreErrorCategory.INTERNAL,
        status=StoreErrorStatus.INTERNAL,
        message="failed",
    )
    batch = ResponseBatch(
        (
            TransactionResponse("ok"),
            TransactionFailureResponse("bad", failure),
            QueryResponse((QueryRow("one", {"value": [1]}),)),
            SingleSubscriptionResponse("s", "instance", 0, ()),
            SingleSubscriptionResponse("s", "instance", 0, None),
            CollectionSubscriptionResponse("c", "instance", 2, ()),
            CollectionSubscriptionResponse("c", "instance", 2, None),
            QuerySubscriptionResponse("q", "instance", {"Many": 2}, ()),
            QuerySubscriptionResponse("q", "instance", {"Many": 2}, None),
            UnsubscribeResponse("c"),
            FailureResponse(failure),
        )
    )
    decoded = decode_response(json.loads(json.dumps(encode_response(batch))))
    assert decoded == batch
    assert decoded.items[3] != decoded.items[4]
    assert decoded.items[5] != decoded.items[6]


def test_commit_and_signal_occurrence_identity_survives_json() -> None:
    event = EventMessage(
        "instance",
        (
            SubscriptionEvent(
                "s",
                {"Single": 4},
                (
                    SetEventAction((("value", 2),)),
                    SetRowEventAction("row", (("value", 3),)),
                    InsertEventAction("new", ()),
                    RemoveEventAction("old"),
                    SignalEventAction("ping", {"a": [1]}, occurrence_id=0),
                    SignalRowEventAction("row", "ping", occurrence_id=1),
                ),
            ),
            SubscriptionEvent("query", {"Many": 1}, ()),
        ),
        transaction_id="transaction",
    )
    encoded = json.loads(json.dumps(encode_event_message(event)))
    assert decode_event_message(encoded) == event


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"requests": []},
        {"requests": {}},
        {"requests": [None]},
        {"requests": [{"type": "unknown"}]},
    ],
)
def test_request_envelope_rejects_malformed_input(payload: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        decode_request(payload)


@pytest.mark.parametrize(
    "action",
    [
        {"type": "set", "schema": "S"},
        {"type": "set", "schema": "S", "values": {"": 1}},
        {"type": "set", "schema": "S", "values": {2: 1}},
        {"type": "set", "schema": "S", "values": []},
        {"type": "insert", "schema": "S", "row_id": "", "values": {}},
        {"type": "remove", "schema": "S", "row_id": 42},
        {"type": "signal", "schema": "S", "signal": ""},
        {"type": "signalrow", "schema": "S", "row_id": "r"},
        {"type": "unknown", "schema": "S"},
        {"type": "set", "schema": "", "values": {"a": 1}},
    ],
)
def test_request_action_trust_boundary(action: dict[str, Any]) -> None:
    with pytest.raises((TypeError, ValueError)):
        decode_request(
            {
                "requests": [
                    {
                        "type": "transaction",
                        "transaction_id": "t",
                        "actions": [action],
                    }
                ]
            }
        )


@pytest.mark.parametrize(
    "cache",
    [
        {"store_instance_id": "i"},
        {"revision": 0},
        {"store_instance_id": "", "revision": 0},
        {"store_instance_id": "i", "revision": -1},
        {"store_instance_id": "i", "revision": True},
    ],
)
def test_schema_cache_requires_instance_and_integer_revision(
    cache: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        decode_request(
            {
                "requests": [
                    {"type": "subscribe_single", "schema": "S", **cache}
                ]
            }
        )


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"responses": []},
        {"responses": {}},
        {"responses": [{"type": "unknown"}]},
        {"responses": [{"type": "transaction", "transaction_id": ""}]},
        {
            "responses": [
                {
                    "type": "query",
                    "rows": [
                        {"id": "x", "values": {}},
                        {"id": "x", "values": {}},
                    ],
                }
            ]
        },
    ],
)
def test_response_envelope_rejects_malformed_input(payload: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        decode_response(payload)


@pytest.mark.parametrize(
    "kind,key",
    [
        ("subscribe_single", "values"),
        ("subscribe_collection", "rows"),
        ("subscribe_query", "rows"),
    ],
)
def test_missing_snapshot_is_not_cached_snapshot(kind: str, key: str) -> None:
    response = {
        "type": kind,
        "subscription_id": "s",
        "store_instance_id": "i",
        "revision": 0,
        "revisions": {"S": 0},
    }
    with pytest.raises(ValueError):
        decode_response({"responses": [response]})
    response[key] = None
    assert decode_response({"responses": [response]}).items


@pytest.mark.parametrize("value", [-1, True, 1.5, "1", None])
def test_response_revision_must_be_nonnegative_integer(value: Any) -> None:
    with pytest.raises(ValueError):
        decode_response(
            {
                "responses": [
                    {
                        "type": "subscribe_single",
                        "subscription_id": "s",
                        "store_instance_id": "i",
                        "revision": value,
                        "values": {},
                    }
                ]
            }
        )


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"event": {}},
        {"event": {"store_instance_id": "i", "events": []}},
        {"event": {"store_instance_id": "", "events": [{}]}},
    ],
)
def test_event_envelope_rejects_malformed_input(payload: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        decode_event_message(payload)


@pytest.mark.parametrize(
    "action",
    [
        {"type": "set", "values": {}},
        {"type": "setrow", "row_id": "", "values": {"x": 1}},
        {"type": "remove", "row_id": 3},
        {"type": "signal", "signal": "ping", "occurrence_id": True},
        {
            "type": "signalrow",
            "signal": "ping",
            "row_id": "x",
            "occurrence_id": -1,
        },
        {"type": "unknown"},
    ],
)
def test_event_action_trust_boundary(action: dict[str, Any]) -> None:
    with pytest.raises((TypeError, ValueError)):
        decode_event_message(
            {
                "event": {
                    "store_instance_id": "i",
                    "events": [
                        {
                            "subscription_id": "s",
                            "revisions": {},
                            "actions": [action],
                        }
                    ],
                }
            }
        )


def test_empty_set_values_decode_for_authoritative_store_validation() -> None:
    requests = RequestBatch(
        (
            TransactionRequest(
                "empty-values",
                (
                    SetRequestAction("Single", ()),
                    SetRowRequestAction("Many", "r", ()),
                ),
            ),
        )
    )
    assert decode_request(encode_request(requests)) == requests
