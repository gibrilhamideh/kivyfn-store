"""Connection cleanup and recovery have one Client owner on both transports."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from kivyfn.store import StoreClient
from kivyfn.store.error import StoreConnectionError, StoreProtocolError
from kivyfn.store.protocol.message.requests import (
    RequestBatch,
    SetRequestAction,
)
from kivyfn.store.protocol.message.responses import ResponseBatch
from kivyfn.store.state.manager import StateManager
from tests.client.test_failure_boundaries import BoundaryFirst, BoundaryStack
from tests.client.test_failure_boundaries import (
    boundary_stack as boundary_stack,
)


class RecoveryClock:
    def __init__(self) -> None:
        self.waits: asyncio.Queue[asyncio.Future[None]] = asyncio.Queue()

    async def sleep(self, seconds: float) -> None:
        assert seconds == 10.0
        release = asyncio.get_running_loop().create_future()
        self.waits.put_nowait(release)
        await release

    async def next(self) -> asyncio.Future[None]:
        async with asyncio.timeout(3):
            return await self.waits.get()


@pytest.fixture
def recovery_clock(monkeypatch: pytest.MonkeyPatch) -> RecoveryClock:
    clock = RecoveryClock()
    monkeypatch.setattr("kivyfn.store.client.client.sleep", clock.sleep)
    return clock


@pytest.mark.parametrize(
    "interval", [0, -1, float("nan"), float("inf"), True, "10"]
)
def test_reconnect_interval_is_validated_once(interval: Any) -> None:
    with pytest.raises(ValueError, match="positive and finite"):
        StoreClient(reconnect_interval=interval)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_waiter", [False, True])
async def test_connect_waits_for_shared_disconnect_cleanup(
    boundary_stack: BoundaryStack,
    monkeypatch: pytest.MonkeyPatch,
    cancel_waiter: bool,
) -> None:
    stack = boundary_stack
    state: Any = stack.state.create(BoundaryFirst)
    await stack.client.connect(stack.endpoint)
    transport = stack.client._require_transport()
    disconnect = transport.disconnect
    entered, release, attempted = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    calls = 0

    async def gated_disconnect() -> None:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        await disconnect()

    async def reconnect() -> None:
        attempted.set()
        await stack.client.connect()

    monkeypatch.setattr(transport, "disconnect", gated_disconnect)
    first = asyncio.create_task(stack.client.disconnect())
    await entered.wait()
    second = asyncio.create_task(stack.client.disconnect())
    connecting = asyncio.create_task(reconnect())
    try:
        await attempted.wait()
        if cancel_waiter:
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
        assert not connecting.done()
        assert not second.done()
        release.set()
        await asyncio.gather(second, connecting)
        assert calls == 1
        assert stack.client.synced and state.synced
        await state.set(value=7)
        assert stack.store.snapshot_by_schema("BoundaryFirst")["value"] == 7
    finally:
        release.set()
        await asyncio.gather(first, second, connecting, return_exceptions=True)


async def lose_connection(stack: BoundaryStack, client: StoreClient) -> None:
    disconnected = asyncio.Event()
    callback = client.on_disconnect

    def observed(error: Exception | None = None) -> None:
        callback(error)
        disconnected.set()

    client.on_disconnect = observed
    client_id = next(iter(stack.host.clients._clients))
    try:
        await stack.host.transport.disconnect(client_id)
        async with asyncio.timeout(3):
            await disconnected.wait()
    finally:
        client.on_disconnect = callback


@pytest.mark.asyncio
async def test_recovery_retries_without_overlap_and_restores_subscription(
    boundary_stack: BoundaryStack,
    recovery_clock: RecoveryClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = boundary_stack
    client = StoreClient(reconnect_interval=10.0)
    manager = StateManager(client)
    state: Any = manager.create(BoundaryFirst)
    try:
        await client.connect(stack.endpoint)
        await state.set(value=3)
        transport = client._require_transport()
        connect = transport.connect
        attempts = 0

        async def fail_twice() -> None:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise StoreConnectionError("injected unavailable endpoint")
            await connect()

        monkeypatch.setattr(transport, "connect", fail_twice)
        await lose_connection(stack, client)
        retry = client._reconnect_task
        assert retry is not None and not state.synced
        for attempt in range(3):
            tick = await recovery_clock.next()
            assert attempts == attempt
            tick.set_result(None)
        await asyncio.wait_for(asyncio.shield(retry), 3)
        assert client.synced and state.synced and state.value == 3
        assert state is manager.create(BoundaryFirst)
        assert len(stack.host.clients) == 1
        assert len(stack.host.subscriptions) == 1
        await state.set(value=4)
        assert stack.store.snapshot_by_schema("BoundaryFirst")["value"] == 4
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_explicit_disconnect_cancels_recovery_and_manual_connect_works(
    boundary_stack: BoundaryStack, recovery_clock: RecoveryClock
) -> None:
    stack = boundary_stack
    client = StoreClient(reconnect_interval=10.0)
    try:
        await client.connect(stack.endpoint)
        await lose_connection(stack, client)
        tick = await recovery_clock.next()
        await client.disconnect()
        assert tick.cancelled()
        assert client._reconnect_task is None
        assert not client.online
        await client.connect()
        assert client.synced
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_manual_connect_shares_active_recovery_attempt(
    boundary_stack: BoundaryStack,
    recovery_clock: RecoveryClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = boundary_stack
    client = StoreClient(reconnect_interval=10.0)
    entered, release = asyncio.Event(), asyncio.Event()
    try:
        await client.connect(stack.endpoint)
        transport = client._require_transport()
        connect = transport.connect
        attempts = 0

        async def gated_connect() -> None:
            nonlocal attempts
            attempts += 1
            entered.set()
            await release.wait()
            await connect()

        monkeypatch.setattr(transport, "connect", gated_connect)
        await lose_connection(stack, client)
        tick = await recovery_clock.next()
        tick.set_result(None)
        await entered.wait()
        retry = client._reconnect_task
        assert retry is not None
        manual = asyncio.create_task(client.connect())
        release.set()
        await asyncio.gather(manual, retry)
        assert attempts == 1 and client.synced
    finally:
        release.set()
        await client.disconnect()


@pytest.mark.asyncio
async def test_initial_failure_is_one_attempt_without_recovery(
    boundary_stack: BoundaryStack,
    recovery_clock: RecoveryClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = boundary_stack
    client = StoreClient(reconnect_interval=10.0)
    from kivyfn.store.transport.endpoint import create_client_transport

    transport = create_client_transport(stack.endpoint, client)
    attempts = 0

    async def fail() -> None:
        nonlocal attempts
        attempts += 1
        raise StoreConnectionError("initial unavailable endpoint")

    monkeypatch.setattr(transport, "connect", fail)
    monkeypatch.setattr(
        "kivyfn.store.client.client.create_client_transport",
        lambda endpoint, owner: transport,
    )
    try:
        with pytest.raises(StoreConnectionError, match="initial unavailable"):
            await client.connect(stack.endpoint)
        assert attempts == 1
        assert client._reconnect_task is None
        assert recovery_clock.waits.empty()
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_incompatible_protocol_stops_recovery_on_both_transports(
    boundary_stack: BoundaryStack,
    recovery_clock: RecoveryClock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stack = boundary_stack
    client = StoreClient(reconnect_interval=10.0)
    try:
        await client.connect(stack.endpoint)
        await lose_connection(stack, client)
        retry = client._reconnect_task
        assert retry is not None
        tick = await recovery_clock.next()
        monkeypatch.setattr(
            "kivyfn.store.transport.direct.client.PROTOCOL_VERSION", 1
        )
        monkeypatch.setattr(
            "kivyfn.store.transport.socketio.client.PROTOCOL_VERSION", 1
        )
        tick.set_result(None)
        await asyncio.wait_for(asyncio.shield(retry), 3)
        assert not client.online
        assert not client._reconnect_enabled
        assert recovery_clock.waits.empty()
        assert "Incompatible Store protocol" in caplog.text
        with pytest.raises(StoreProtocolError):
            await client.connect()
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_recovery_does_not_replay_interrupted_committed_mutation(
    boundary_stack: BoundaryStack,
    recovery_clock: RecoveryClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = boundary_stack
    client = StoreClient(reconnect_interval=10.0)
    state: Any = StateManager(client).create(BoundaryFirst)
    committed, release = asyncio.Event(), asyncio.Event()
    try:
        await client.connect(stack.endpoint)
        transport = client._require_transport()
        request = transport.request
        calls = 0

        async def gated(batch: RequestBatch) -> ResponseBatch:
            nonlocal calls
            calls += 1
            response = await request(batch)
            committed.set()
            await release.wait()
            return response

        monkeypatch.setattr(transport, "request", gated)
        operation = client.transaction(
            transaction_id="interrupted-committed",
            actions=(SetRequestAction("BoundaryFirst", (("value", 9),)),),
        )
        await committed.wait()
        await lose_connection(stack, client)
        with pytest.raises(StoreConnectionError):
            await operation
        assert calls == 1
        monkeypatch.setattr(transport, "request", request)
        release.set()
        retry = client._reconnect_task
        assert retry is not None
        tick = await recovery_clock.next()
        tick.set_result(None)
        await asyncio.wait_for(asyncio.shield(retry), 3)
        assert client.synced and state.value == 9
        assert client._futures.empty
        assert calls == 1
        assert stack.store.schema_revisions["BoundaryFirst"] == 1
    finally:
        release.set()
        await client.disconnect()


@pytest.mark.asyncio
async def test_recovery_from_replacement_host_refreshes_existing_state(
    boundary_stack: BoundaryStack, recovery_clock: RecoveryClock, tmp_path
) -> None:
    from kivyfn.store import Store, StoreHost
    from kivyfn.store.builder import Branch, Builder
    from kivyfn.store.builder.config import HostConfig

    stack = boundary_stack
    client = StoreClient(reconnect_interval=10.0)
    state: Any = StateManager(client).create(BoundaryFirst)
    config = (
        Builder()
        .tree(Branch(BoundaryFirst))
        .storage_path(tmp_path / "replacement")
        .host(HostConfig(stack.endpoint))
        .build()
    )
    replacement = Store(config)
    host = None
    try:
        await client.connect(stack.endpoint)
        await state.set(value=1)
        instance = client._store_instance_id
        await stack.host.stop()
        tick = await recovery_clock.next()
        retry = client._reconnect_task
        assert retry is not None
        replacement.boot()
        result = replacement.execute(
            (SetRequestAction("BoundaryFirst", (("value", 8),)),),
            transaction_id="replacement-data",
        )
        assert result.success
        # A stable endpoint is owned by the replacement process/Host.
        host = StoreHost(replacement)
        await host.start()
        tick.set_result(None)
        await asyncio.wait_for(asyncio.shield(retry), 3)
        assert client._store_instance_id != instance
        assert state.value == 8 and state.synced
        await state.set(value=9)
        assert replacement.snapshot_by_schema("BoundaryFirst")["value"] == 9
    finally:
        await client.disconnect()
        if host is not None:
            await host.stop()
        if replacement.booted:
            replacement.shutdown()


@pytest.mark.asyncio
async def test_explicit_disconnect_stops_an_active_recovery_attempt(
    boundary_stack: BoundaryStack,
    recovery_clock: RecoveryClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = boundary_stack
    client = StoreClient(reconnect_interval=10.0)
    entered, cancelled = asyncio.Event(), asyncio.Event()
    try:
        await client.connect(stack.endpoint)
        transport = client._require_transport()

        async def stalled_connect() -> None:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        monkeypatch.setattr(transport, "connect", stalled_connect)
        await lose_connection(stack, client)
        tick = await recovery_clock.next()
        tick.set_result(None)
        await entered.wait()
        await client.disconnect()
        assert cancelled.is_set()
        assert not client.online and client._reconnect_task is None
        assert recovery_clock.waits.empty()
        assert client._connect_task is None
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_rejected_subscription_does_not_reconnect_healthy_peers(
    boundary_stack: BoundaryStack, recovery_clock: RecoveryClock
) -> None:
    from kivyfn.store.schema import NumericField, SingleSchema

    class AbsentSchema(SingleSchema):
        value = NumericField(default=0)

    stack = boundary_stack
    client = StoreClient(reconnect_interval=10.0)
    manager = StateManager(client)
    healthy: Any = manager.create(BoundaryFirst)
    try:
        await client.connect(stack.endpoint)
        await lose_connection(stack, client)
        tick = await recovery_clock.next()
        rejected = manager.create(AbsentSchema)
        retry = client._reconnect_task
        assert retry is not None
        tick.set_result(None)
        await asyncio.wait_for(asyncio.shield(retry), 3)
        assert client.online and not client.synced
        assert healthy.synced and not rejected.synced
        assert recovery_clock.waits.empty()
        assert len(stack.host.clients) == 1
        assert len(stack.host.subscriptions) == 1
        await healthy.set(value=6)
        assert stack.store.snapshot_by_schema("BoundaryFirst")["value"] == 6
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_initial_connection_lost_before_return_is_not_successful(
    boundary_stack: BoundaryStack,
    recovery_clock: RecoveryClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kivyfn.store.transport.endpoint import create_client_transport

    stack = boundary_stack
    client = StoreClient(reconnect_interval=10.0)
    transport = create_client_transport(stack.endpoint, client)
    connect = transport.connect
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_return() -> None:
        await connect()
        entered.set()
        await release.wait()

    monkeypatch.setattr(transport, "connect", delayed_return)
    monkeypatch.setattr(
        "kivyfn.store.client.client.create_client_transport",
        lambda endpoint, owner: transport,
    )
    connecting = asyncio.create_task(client.connect(stack.endpoint))
    try:
        await entered.wait()
        await lose_connection(stack, client)
        release.set()
        with pytest.raises(StoreConnectionError, match="synchronization"):
            await connecting
        assert not client.online
        assert client._reconnect_task is None
        assert recovery_clock.waits.empty()
    finally:
        release.set()
        await asyncio.gather(connecting, return_exceptions=True)
        await client.disconnect()
