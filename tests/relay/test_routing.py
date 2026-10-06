from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from kivyfn.store.codec.events import decode_event_message
from kivyfn.store.codec.responses import decode_response
from kivyfn.store.protocol.message.event import SignalEventAction
from kivyfn.store.protocol.message.requests import (
    CollectionSubscriptionRequest,
    InsertRequestAction,
    QueryRequest,
    QuerySubscriptionRequest,
    SignalRequestAction,
    SingleSubscriptionRequest,
    TransactionRequest,
    UnsubscribeRequest,
)
from kivyfn.store.protocol.message.responses import (
    QueryResponse,
    TransactionFailureResponse,
    TransactionResponse,
)
from kivyfn.store.query import Query
from tests.relay.helpers import RelayRow, Stack

pytestmark = pytest.mark.asyncio


async def test_registration_and_distinct_subscription_owners(
    relay_stack: Stack,
) -> None:
    stack = relay_stack
    auth = stack.gateway.credentials[-1]
    assert auth["role"] == "store"
    assert auth["store_id"] == "application-001"
    assert auth["credentials"] == {"token": "test-only"}
    first, second = await stack.peer(1), await stack.peer(2)
    subscribe = SingleSubscriptionRequest("RelayRoot")
    one, two = await asyncio.gather(
        first.response(subscribe), second.response(subscribe)
    )
    sub_one = one["responses"][0]["subscription_id"]
    sub_two = two["responses"][0]["subscription_id"]
    assert sub_one != sub_two
    await second.response(UnsubscribeRequest(sub_one))
    # Unsubscribe is idempotent, but cannot remove another client's source.
    assert sub_one in stack.host.subscriptions._by_id
    tx = first.transaction(12)
    response = await first.response(tx)
    assert decode_response(response).items == (
        TransactionResponse(tx.transaction_id),
    )
    event = decode_event_message((await second.next())["payload"])
    assert event.transaction_id == tx.transaction_id
    assert event.events[0].subscription_id == sub_two
    assert event.events[0].revisions == {"RelayRoot": 1}
    await first.response(UnsubscribeRequest(sub_one))
    assert len(stack.host.clients) == 2


async def test_event_before_confirmation_and_other_client_independence(
    relay_stack: Stack,
) -> None:
    stack = relay_stack
    first, second = await stack.peer(1), await stack.peer(2)
    await first.response(SingleSubscriptionRequest("RelayRoot"))
    gate = asyncio.Event()
    stack.gateway.gates[(1, "event")] = gate
    tx = first.transaction(10)
    assert await first.submit(tx) == {"accepted": True}
    event = await first.next()
    assert event["type"] == "event"
    assert stack.store.snapshot_by_schema("RelayRoot")["value"] == 10
    assert stack.gateway.frames[(first.sid, 1)].empty()
    other = await second.response(second.transaction(20))
    assert isinstance(decode_response(other).items[0], TransactionResponse)
    gate.set()
    frames = [await first.next(), await first.next()]
    assert sorted(frame["type"] for frame in frames) == ["event", "response"]
    response = next(frame for frame in frames if frame["type"] == "response")
    assert response["sequence"] > event["sequence"]
    assert decode_response(response["payload"]).items == (
        TransactionResponse(tx.transaction_id),
    )
    sequences = [
        f["sequence"] for f in stack.gateway.seen if f["channel_id"] == 1
    ]
    assert sequences == list(range(len(sequences)))


async def test_simultaneous_guarded_writes_have_one_winner(
    relay_stack: Stack,
) -> None:
    first, second = await relay_stack.peer(1), await relay_stack.peer(2)
    subscribed = await first.response(SingleSubscriptionRequest("RelayRoot"))
    snapshot = subscribed["responses"][0]
    transactions = [
        replace(
            peer.transaction(value),
            expected_store_instance_id=snapshot["store_instance_id"],
            expected_revisions={"RelayRoot": snapshot["revision"]},
        )
        for peer, value in ((first, 11), (second, 22))
    ]
    responses = await asyncio.gather(
        first.response(transactions[0]), second.response(transactions[1])
    )
    results = [decode_response(response).items[0] for response in responses]
    assert sum(isinstance(item, TransactionResponse) for item in results) == 1
    assert (
        sum(isinstance(item, TransactionFailureResponse) for item in results)
        == 1
    )


async def test_structured_validation_failure_and_atomic_batch(
    relay_stack: Stack,
) -> None:
    peer = await relay_stack.peer(1)
    await peer.response(SingleSubscriptionRequest("RelayRoot"))
    tx = peer.transaction(-5)
    failure = decode_response(await peer.response(tx)).items[0]
    assert isinstance(failure, TransactionFailureResponse)
    assert failure.transaction_id == tx.transaction_id
    assert relay_stack.store.snapshot_by_schema("RelayRoot")["value"] == 0
    assert not any(f["type"] == "event" for f in relay_stack.gateway.seen)
    batch = (peer.transaction(3), peer.transaction(4))
    assert await peer.submit(*batch) == {"accepted": True}
    first, second, response = (
        await peer.next(),
        await peer.next(),
        await peer.next(),
    )
    assert [first["type"], second["type"], response["type"]] == [
        "event",
        "event",
        "response",
    ]
    assert decode_response(response["payload"]).items == tuple(
        TransactionResponse(tx.transaction_id) for tx in batch
    )


async def test_transaction_namespace_cannot_cross_clients(
    relay_stack: Stack,
) -> None:
    first, second = await relay_stack.peer(1), await relay_stack.peer(2)
    tx = first.transaction(40)
    await first.response(tx)
    # Validate the entire batch before running any part of it.
    assert await second.submit(second.transaction(50), tx) == {
        "accepted": True
    }
    closed = await second.next()
    assert closed["type"] == "closed"
    assert closed["payload"]["code"] == "invalid_request"
    assert relay_stack.store.snapshot_by_schema("RelayRoot")["value"] == 40
    await first.response(first.transaction(41))


async def test_duplicate_transaction_does_not_repeat_named_signal(
    relay_stack: Stack,
) -> None:
    peer = await relay_stack.peer(1)
    await peer.response(SingleSubscriptionRequest("RelayRoot"))
    tx = TransactionRequest(
        peer.prefix + "ping",
        (SignalRequestAction("RelayRoot", "ping", {"n": 1}),),
    )
    first = await peer.response(tx)
    second = await peer.response(tx)
    assert first == second
    events = [f for f in relay_stack.gateway.seen if f["type"] == "event"]
    assert len(events) == 1
    event = decode_event_message(events[0]["payload"]).events[0]
    assert event.revisions == {}
    action = event.actions[0]
    assert isinstance(action, SignalEventAction)
    assert action.signal == "ping"
    assert action.occurrence_id is not None
    changed = await peer.response(peer.transaction(2, nonce="ping"))
    assert isinstance(
        decode_response(changed).items[0], TransactionFailureResponse
    )


async def test_collections_queries_and_local_writes_use_same_host(
    relay_stack: Stack,
) -> None:
    peer = await relay_stack.peer(1)
    query = Query(RelayRow)
    replies = decode_response(
        await peer.response(
            CollectionSubscriptionRequest("RelayRow"),
            QuerySubscriptionRequest(query),
        )
    )
    assert len(replies.items) == 2
    result = relay_stack.store.execute(
        (InsertRequestAction("RelayRow", "r1", (("label", "local"),)),),
        transaction_id="local-automation",
    )
    assert result.success
    event = decode_event_message((await peer.next())["payload"])
    assert event.transaction_id == "local-automation"
    assert len(event.events) == 2
    response = decode_response(await peer.response(QueryRequest(query)))
    item = response.items[0]
    assert isinstance(item, QueryResponse)
    assert len(item.rows) == 1
