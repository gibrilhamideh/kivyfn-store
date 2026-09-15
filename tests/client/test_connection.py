from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from kivyfn.store import Store, StoreClient, StoreHost
from kivyfn.store.builder import Branch, Builder
from kivyfn.store.error import StoreConnectionError
from kivyfn.store.protocol.message.requests import (
    RequestBatch,
    SetRequestAction,
)
from kivyfn.store.protocol.message.responses import ResponseBatch
from kivyfn.store.state.manager import StateManager
from kivyfn.store.transport.client import ClientTransport
from kivyfn.store.transport.endpoint import create_client_transport
from tests.client.test_failure_boundaries import BoundaryFirst, BoundaryStack
from tests.client.test_failure_boundaries import (
    boundary_stack as boundary_stack,
)
from tests.conftest import TransportKind
from tests.helpers import intercept_client_requests


def test_client_and_state_can_be_created_without_an_event_loop() -> None:
    client = StoreClient()
    manager = StateManager(client)
    state = manager.create(BoundaryFirst)
    assert client.endpoint is None
    assert client._transport is None
    assert client._loop is None
    assert not client.online and not client.synced and not state.synced
    assert len(client._subscriptions) == 1
    with pytest.raises(StoreConnectionError, match="no transport"):
        client._require_transport()


@pytest.mark.asyncio
async def test_unused_disconnect_preserves_desired_subscriptions(
    boundary_stack: BoundaryStack,
) -> None:
    stack = boundary_stack
    state = stack.state.create(BoundaryFirst)
    await stack.client.disconnect()
    await stack.client.disconnect()
    assert stack.client.endpoint is None
    assert stack.client._transport is None
    assert len(stack.client._subscriptions) == 1
    await stack.client.connect(stack.endpoint)
    assert state.synced and stack.client.synced


@pytest.mark.asyncio
async def test_initial_connect_requires_endpoint_without_claiming_loop(
    boundary_stack: BoundaryStack,
) -> None:
    client = boundary_stack.client
    with pytest.raises(StoreConnectionError, match="endpoint is required"):
        await client.connect()
    assert client.endpoint is None and client._loop is None
    await client.connect(boundary_stack.endpoint)
    assert client.online and client.synced


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint", ["", "direct://store/path", "http://127.0.0.1:0"]
)
async def test_invalid_initial_endpoint_leaves_client_usable(
    boundary_stack: BoundaryStack, endpoint: str
) -> None:
    client = boundary_stack.client
    with pytest.raises(ValueError):
        await client.connect(endpoint)
    assert client.endpoint is None
    assert client._transport is None and client._loop is None
    await client.connect(boundary_stack.endpoint)
    assert client.synced


@pytest.mark.asyncio
async def test_invalid_endpoint_switch_preserves_previous_connection_target(
    boundary_stack: BoundaryStack,
) -> None:
    stack = boundary_stack
    state = stack.state.create(BoundaryFirst)
    await stack.client.connect(stack.endpoint)
    transport = stack.client._require_transport()
    await stack.client.disconnect()
    with pytest.raises(ValueError):
        await stack.client.connect("file:///invalid-store")
    assert stack.client.endpoint == stack.endpoint
    assert stack.client._require_transport() is transport
    assert stack.client._loop is None
    await stack.client.connect()
    assert state.synced and stack.client.synced


@pytest.mark.asyncio
async def test_repeated_connect_and_reconnect_reuse_transport(
    boundary_stack: BoundaryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = boundary_stack
    state: Any = stack.state.create(BoundaryFirst)
    created: list[ClientTransport] = []

    def create(endpoint: str, owner: StoreClient) -> ClientTransport:
        transport = create_client_transport(endpoint, owner)
        created.append(transport)
        return transport

    monkeypatch.setattr(
        "kivyfn.store.client.client.create_client_transport", create
    )
    await stack.client.connect(stack.endpoint)
    await stack.client.connect(stack.endpoint)
    await stack.client.connect()
    await state.set(value=4)
    await stack.client.disconnect()
    assert not state.synced and not created[0].connected
    assert stack.client.endpoint == stack.endpoint
    await stack.client.connect()
    assert created == [stack.client._require_transport()]
    assert state.synced and state.value == 4
    await state.set(value=5)
    assert stack.store.snapshot_by_schema("BoundaryFirst")["value"] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("during_sync", [False, True])
async def test_endpoint_change_requires_disconnect_even_during_sync(
    boundary_stack: BoundaryStack,
    monkeypatch: pytest.MonkeyPatch,
    during_sync: bool,
) -> None:
    stack = boundary_stack
    state = stack.state.create(BoundaryFirst)
    requests = intercept_client_requests(monkeypatch, stack.client)
    reached, release = asyncio.Event(), asyncio.Event()

    async def gated(request: RequestBatch) -> ResponseBatch:
        response = await requests.send(request)
        reached.set()
        await release.wait()
        return response

    requests.request = gated
    connecting = asyncio.create_task(stack.client.connect(stack.endpoint))
    try:
        await reached.wait()
        transport = stack.client._require_transport()
        if not during_sync:
            release.set()
            await connecting
        with pytest.raises(StoreConnectionError, match="Disconnect before"):
            await stack.client.connect(f"direct://other-{uuid4().hex}")
        assert stack.client.endpoint == stack.endpoint
        assert stack.client._require_transport() is transport
        release.set()
        await connecting
        assert stack.client.synced and state.synced
    finally:
        release.set()
        await asyncio.gather(connecting, return_exceptions=True)


@pytest.mark.asyncio
async def test_disconnect_during_initial_sync_allows_remembered_reconnect(
    boundary_stack: BoundaryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = boundary_stack
    state = stack.state.create(BoundaryFirst)
    requests = intercept_client_requests(monkeypatch, stack.client)
    reached, release = asyncio.Event(), asyncio.Event()

    async def gated(request: RequestBatch) -> ResponseBatch:
        response = await requests.send(request)
        reached.set()
        await release.wait()
        return response

    requests.request = gated
    connecting = asyncio.create_task(stack.client.connect(stack.endpoint))
    try:
        await reached.wait()
        await stack.client.disconnect()
        with pytest.raises(asyncio.CancelledError):
            await connecting
        assert not state.synced and not stack.client.online
        assert not stack.client._require_transport().connected
        release.set()
        await stack.client.connect()
        assert state.synced and stack.client.synced
    finally:
        release.set()
        await asyncio.gather(connecting, return_exceptions=True)


@pytest.mark.asyncio
async def test_failed_connection_retains_endpoint_for_retry(
    boundary_stack: BoundaryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = boundary_stack
    state = stack.state.create(BoundaryFirst)
    calls = 0

    def create(endpoint: str, owner: StoreClient) -> ClientTransport:
        transport = create_client_transport(endpoint, owner)
        connect = transport.connect

        async def fail_once() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise StoreConnectionError("injected connection failure")
            await connect()

        transport.connect = fail_once
        return transport

    monkeypatch.setattr(
        "kivyfn.store.client.client.create_client_transport", create
    )
    with pytest.raises(StoreConnectionError, match="injected"):
        await stack.client.connect(stack.endpoint)
    transport = stack.client._require_transport()
    assert not transport.connected and not state.synced
    assert stack.client.endpoint == stack.endpoint
    await stack.client.connect()
    assert calls == 2 and stack.client._require_transport() is transport
    assert state.synced and stack.client.synced


@pytest.mark.asyncio
@pytest.mark.parametrize("next_transport", ["direct", "socketio"])
async def test_endpoint_switch_refreshes_same_revision_from_different_store(
    boundary_stack: BoundaryStack,
    tmp_path: Path,
    next_transport: TransportKind,
) -> None:
    stack = boundary_stack
    state: Any = stack.state.create(BoundaryFirst)
    await stack.client.connect(stack.endpoint)
    await state.set(value=1)
    previous = stack.client._require_transport()
    old_instance = stack.client._store_instance_id
    old_revisions = dict(stack.store.schema_revisions)
    await stack.client.disconnect()

    root, config = (
        Builder()
        .tree(Branch(BoundaryFirst))
        .storage_path(tmp_path / "second")
        .build()
    )
    store = Store(root, config)
    endpoint = (
        f"direct://second-{uuid4().hex}"
        if next_transport == "direct"
        else "http://127.0.0.1:0"
    )
    host = StoreHost(store, endpoint)
    try:
        store.boot()
        result = store.execute(
            (SetRequestAction("BoundaryFirst", (("value", 8),)),),
            transaction_id="second-store-value",
        )
        assert result.success
        assert old_revisions["BoundaryFirst"] == 1
        assert store.schema_revisions["BoundaryFirst"] == 1
        await host.start()
        endpoint = host.transport.endpoint.get("endpoint", host.endpoint)
        await stack.client.connect(endpoint)
        assert stack.client._require_transport() is not previous
        assert not previous.connected
        assert stack.client.endpoint == endpoint
        assert stack.client._store_instance_id != old_instance
        assert state is stack.state.create(BoundaryFirst)
        assert state.synced and state.value == 8
        await state.set(value=9)
        assert store.snapshot_by_schema("BoundaryFirst")["value"] == 9
        assert stack.store.snapshot_by_schema("BoundaryFirst")["value"] == 1
    finally:
        await stack.client.disconnect()
        await host.stop()
        store.shutdown()
