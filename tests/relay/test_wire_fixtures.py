from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kivyfn.store.codec.events import (
    decode_event_message,
    encode_event_message,
)
from kivyfn.store.codec.requests import decode_request, encode_request
from kivyfn.store.codec.responses import decode_response, encode_response
from kivyfn.store.protocol.message.requests import (
    SetRequestAction,
    SignalRequestAction,
    SingleSubscriptionRequest,
    TransactionRequest,
)
from tests.relay.helpers import Stack

FIXTURE = Path(__file__).parent / "fixtures" / "store-v3.json"


async def wire_conversation(stack: Stack) -> dict[str, Any]:
    peer = await stack.peer(1)
    transactions = [
        SingleSubscriptionRequest("RelayRoot"),
        TransactionRequest(
            peer.prefix + "update",
            (
                SetRequestAction("RelayRoot", (("value", 5),)),
                SignalRequestAction(
                    "RelayRoot", "ping", {"source": "fixture"}
                ),
            ),
        ),
        peer.transaction(-1, nonce="invalid"),
        peer.transaction(5, nonce="noop"),
    ]
    requests = []
    subscription = ""
    instance = ""
    for request in transactions:
        from kivyfn.store.protocol.message.requests import RequestBatch

        requests.append(encode_request(RequestBatch((request,))))
        response = await peer.response(request)
        if not subscription:
            item = response["responses"][0]
            subscription = item["subscription_id"]
            instance = item["store_instance_id"]
    text = json.dumps(
        {
            "relay_version": 1,
            "protocol_version": 3,
            "requests": requests,
            "frames": stack.gateway.seen,
        }
    )
    return json.loads(
        text.replace(stack.generation, "GENERATION")
        .replace(instance, "STORE_INSTANCE")
        .replace(subscription, "SUBSCRIPTION")
    )


@pytest.mark.asyncio
async def test_wire_transcript_matches_language_neutral_fixture(
    relay_stack: Stack,
) -> None:
    assert await wire_conversation(relay_stack) == json.loads(
        FIXTURE.read_text()
    )


def test_fixture_uses_unchanged_store_codecs() -> None:
    fixture = json.loads(FIXTURE.read_text())
    for request in fixture["requests"]:
        assert encode_request(decode_request(request)) == request
    for frame in fixture["frames"]:
        if frame["type"] == "response":
            value = frame["payload"]
            assert encode_response(decode_response(value)) == value
        elif frame["type"] == "event":
            value = frame["payload"]
            assert encode_event_message(decode_event_message(value)) == value
