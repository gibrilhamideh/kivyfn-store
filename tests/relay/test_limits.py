from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kivyfn.store.protocol.message.requests import (
    SetRequestAction,
    SingleSubscriptionRequest,
)
from tests.relay.helpers import Gateway, Peer, Stack, running_stack

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("limit", ["messages", "bytes"])
async def test_incoming_backlog_is_bounded_and_isolated(
    tmp_path: Path, gateway: Gateway, limit: str
) -> None:
    config = (
        {"max_pending_messages": 1}
        if limit == "messages"
        else {"max_pending_bytes": 400}
    )
    async with running_stack(tmp_path, gateway, **config) as stack:
        slow, healthy = await stack.peer(1), await stack.peer(2)
        await slow.response(SingleSubscriptionRequest("RelayRoot"))
        gateway.gates[(1, "event")] = asyncio.Event()
        await slow.submit(slow.transaction(1))
        assert (await slow.next())["type"] == "event"
        assert await slow.submit(slow.transaction(2)) == {"accepted": True}
        receipt = await slow.submit(slow.transaction(3))
        assert receipt == {"accepted": False, "code": "queue_full"}
        assert (await slow.next())["type"] == "closed"
        await healthy.response(healthy.transaction(4))
        assert stack.store.snapshot_by_schema("RelayRoot")["value"] == 4
        assert stack.relay.online


async def test_outgoing_backlog_disconnects_only_slow_client(
    tmp_path: Path, gateway: Gateway
) -> None:
    async with running_stack(
        tmp_path, gateway, max_pending_messages=1
    ) as stack:
        slow, healthy = await stack.peer(1), await stack.peer(2)
        await slow.response(SingleSubscriptionRequest("RelayRoot"))
        gateway.gates[(1, "event")] = asyncio.Event()
        await slow.submit(slow.transaction(1))
        assert (await slow.next())["type"] == "event"
        for value in (2, 3):
            result = stack.store.execute(
                (SetRequestAction("RelayRoot", (("value", value),)),),
                transaction_id=f"local-{value}",
            )
            assert result.success
        closed = await slow.next()
        assert closed["type"] == "closed"
        await healthy.response(healthy.transaction(4))
        assert stack.relay.online


async def test_rejected_or_missing_receipt_never_confirms_commit(
    tmp_path: Path, gateway: Gateway
) -> None:
    async with running_stack(tmp_path, gateway, request_timeout=0.04) as stack:
        peer = await stack.peer(1)
        await peer.response(SingleSubscriptionRequest("RelayRoot"))
        gateway.gates[(1, "event")] = asyncio.Event()
        await peer.submit(peer.transaction(8))
        assert (await peer.next())["type"] == "event"
        assert (await peer.next())["type"] == "closed"
        assert stack.store.snapshot_by_schema("RelayRoot")["value"] == 8
        assert not any(
            f["type"] == "response" and f.get("request_sequence") == 2
            for f in gateway.seen
        )
        assert stack.relay.online


async def test_client_limit_and_nonreusable_channel_numbers(
    tmp_path: Path, gateway: Gateway
) -> None:
    async with running_stack(tmp_path, gateway, max_clients=1) as stack:
        first = await stack.peer(1)
        second = Peer(gateway, stack.sid, stack.generation, 2)
        assert await second.send("open", {"protocol_version": 3}) == {
            "accepted": False,
            "code": "client_limit",
        }
        await first.send("close")
        assert (await first.next())["type"] == "closed"
        await asyncio.sleep(0.01)
        await stack.peer(3)
        assert await first.send(
            "open", {"protocol_version": 3}, sequence=0
        ) == {"accepted": False, "code": "invalid_sequence"}


@pytest.mark.parametrize(
    "case", ["gap", "duplicate", "malformed", "namespace"]
)
async def test_invalid_channel_requests_do_not_affect_other_clients(
    relay_stack: Stack, case: str
) -> None:
    bad, good = await relay_stack.peer(1), await relay_stack.peer(2)
    if case == "gap":
        receipt = await bad.send("request", {}, sequence=7)
        assert receipt["accepted"] is False
    elif case == "duplicate":
        receipt = await bad.send("open", {"protocol_version": 3}, sequence=0)
        assert receipt["accepted"] is False
    elif case == "malformed":
        await bad.send("request", {"requests": [{}]})
    else:
        await bad.submit(good.transaction(9))
    assert (await bad.next())["type"] == "closed"
    await good.response(good.transaction(5))
    assert relay_stack.relay.online


@pytest.mark.parametrize(
    "frame", [None, {"type": "request"}, {"blob": "x" * 2048}]
)
async def test_bad_physical_frames_fence_all_channels(
    tmp_path: Path, gateway: Gateway, frame: object
) -> None:
    async with running_stack(tmp_path, gateway, max_frame_bytes=1024) as stack:
        await stack.peer(1)
        receipt = await gateway.send(stack.sid, frame)
        assert receipt["accepted"] is False
        _, registration = await gateway.registration()
        await stack.relay.wait_online(timeout=2)
        assert registration["generation"] != stack.generation
        assert len(stack.host.clients) == 0
