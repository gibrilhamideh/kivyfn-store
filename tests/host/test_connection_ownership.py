"""Each connection owns its delivery and close path independently."""

from __future__ import annotations

import asyncio

import pytest

from kivyfn.store.error import StoreProtocolError, StoreStateError
from kivyfn.store.protocol.message.requests import (
    CollectionSubscriptionRequest,
    InsertRequestAction,
    RequestBatch,
)
from kivyfn.store.protocol.version import PROTOCOL_VERSION
from tests.conftest import StateStack


@pytest.mark.asyncio
async def test_extra_connection_uses_its_own_delivery_and_close(
    state_stack: StateStack,
) -> None:
    stack = state_stack
    messages, closed = [], []
    received = asyncio.Event()

    async def send(client_id, message):
        messages.append((client_id, message))
        received.set()

    async def close(client_id):
        closed.append(client_id)

    await stack.host.connect(
        "browser-style",
        send=send,
        close=close,
        protocol_version=PROTOCOL_VERSION,
    )
    await stack.host.request(
        "browser-style",
        RequestBatch((CollectionSubscriptionRequest("UserSchema"),)),
    )
    await stack.client.connect(stack.endpoint)
    response = await stack.client.transaction(
        transaction_id="application-change",
        actions=(
            InsertRequestAction(
                "UserSchema", "first", (("email", "first@example.com"),)
            ),
        ),
    )
    assert response.transaction_id == "application-change"
    await received.wait()
    assert messages[0][0] == "browser-style"
    assert closed == []
    await stack.host.stop()
    assert closed == ["browser-style"]
    assert not stack.client.online


@pytest.mark.asyncio
async def test_failed_extra_delivery_closes_only_that_owner(
    state_stack: StateStack,
) -> None:
    stack = state_stack
    closed = asyncio.Event()

    async def send(client_id, message):
        raise RuntimeError("browser writer failed")

    async def close(client_id):
        closed.set()

    await stack.host.connect(
        "failed-browser",
        send=send,
        close=close,
        protocol_version=PROTOCOL_VERSION,
    )
    await stack.host.request(
        "failed-browser",
        RequestBatch((CollectionSubscriptionRequest("UserSchema"),)),
    )
    await stack.client.connect(stack.endpoint)
    await stack.client.transaction(
        transaction_id="healthy-write",
        actions=(
            InsertRequestAction(
                "UserSchema", "first", (("email", "first@example.com"),)
            ),
        ),
    )
    await closed.wait()
    assert stack.host.running
    assert stack.client.online


@pytest.mark.asyncio
async def test_incompatible_and_duplicate_clients_are_rejected(
    state_stack: StateStack,
) -> None:
    host = state_stack.host
    with pytest.raises(
        StoreProtocolError, match="Incompatible Store protocol"
    ):
        await host.connect("missing-version")
    with pytest.raises(
        StoreProtocolError, match="Incompatible Store protocol"
    ):
        await host.connect("old-version", protocol_version=1)
    assert host.clients.get("old-version") is None
    await host.connect("registered", protocol_version=PROTOCOL_VERSION)
    with pytest.raises(StoreStateError, match="already registered"):
        await host.connect("registered", protocol_version=PROTOCOL_VERSION)
    await host.disconnect("registered")


@pytest.mark.asyncio
async def test_hosted_mutation_schedules_entire_execution_on_owner_loop(
    state_stack: StateStack,
) -> None:
    stack = state_stack
    action = InsertRequestAction(
        "UserSchema", "worker", (("email", "worker@example.com"),)
    )
    with pytest.raises(StoreStateError, match="event loop"):
        await asyncio.to_thread(
            stack.store.execute, (action,), transaction_id="worker-wrong"
        )
    loop = asyncio.get_running_loop()

    async def mutate():
        return stack.store.execute((action,), transaction_id="worker-right")

    def worker():
        return asyncio.run_coroutine_threadsafe(mutate(), loop).result(5)

    result = await asyncio.to_thread(worker)
    assert result.success
    assert "worker" in stack.store.snapshot_by_schema("UserSchema")


@pytest.mark.asyncio
async def test_application_transport_rejects_old_protocol(
    state_stack: StateStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kivyfn.store.transport.direct import client as direct_client
    from kivyfn.store.transport.socketio import client as socketio_client

    monkeypatch.setattr(direct_client, "PROTOCOL_VERSION", 1)
    monkeypatch.setattr(socketio_client, "PROTOCOL_VERSION", 1)
    with pytest.raises(
        StoreProtocolError, match="Incompatible Store protocol"
    ):
        await state_stack.client.connect(state_stack.endpoint)
    assert not state_stack.client.online
    assert len(state_stack.host.clients) == 0
    assert state_stack.host.running


@pytest.mark.asyncio
async def test_connection_cannot_create_delivery_tasks_on_worker_loop(
    state_stack: StateStack,
) -> None:
    async def register():
        await state_stack.host.connect(
            "wrong-loop", protocol_version=PROTOCOL_VERSION
        )

    with pytest.raises(StoreStateError, match="another event loop"):
        await asyncio.to_thread(lambda: asyncio.run(register()))
    assert state_stack.host.clients.get("wrong-loop") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["start", "stop"])
async def test_wrong_loop_lifecycle_has_no_connection_or_listener_effects(
    state_stack: StateStack, method: str
) -> None:
    stack = state_stack
    await stack.client.connect(stack.endpoint)
    clients = tuple(stack.host.clients._clients)
    transport_endpoint = dict(stack.host.transport.endpoint)
    original_close = stack.host._close_task

    async def wrong_loop() -> None:
        await getattr(stack.host, method)()

    with pytest.raises(StoreStateError, match="another event loop"):
        await asyncio.to_thread(lambda: asyncio.run(wrong_loop()))
    assert stack.host.running and stack.host.transport.running
    assert stack.host._close_task is original_close
    assert tuple(stack.host.clients._clients) == clients
    assert stack.host.transport.endpoint == transport_endpoint
    response = await stack.client.transaction(
        transaction_id="after-rejected-lifecycle",
        actions=(
            InsertRequestAction(
                "UserSchema", "safe", (("email", "safe@example.com"),)
            ),
        ),
    )
    assert response.transaction_id == "after-rejected-lifecycle"
    assert stack.store.snapshot_by_schema("UserSchema")["safe"]
