from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio

from kivyfn.store import Store, StoreClient, StoreHost
from kivyfn.store.builder import Branch, Builder
from kivyfn.store.builder.config import HostConfig
from kivyfn.store.error import (
    InvalidOperationError,
    StoreConnectionError,
    StoreProtocolError,
    StoreTimeoutError,
)
from kivyfn.store.protocol.message.event import (
    EventMessage,
    SetEventAction,
    SubscriptionEvent,
)
from kivyfn.store.protocol.message.requests import (
    InsertRequestAction,
    RequestBatch,
    SetRequestAction,
    SetRowRequestAction,
    SignalRequestAction,
)
from kivyfn.store.protocol.message.responses import (
    ResponseBatch,
    SingleSubscriptionResponse,
    TransactionFailureResponse,
    TransactionResponse,
    UnsubscribeResponse,
)
from kivyfn.store.schema import ManySchema, NumericField, Signal, SingleSchema
from kivyfn.store.state.manager import StateManager
from kivyfn.store.transport.socketio.client import SocketIOClientTransport
from tests.conftest import TransportKind
from tests.helpers import intercept_client_requests


class BoundaryFirst(SingleSchema):
    value = NumericField(default=0)
    changed = Signal()


class BoundarySecond(SingleSchema):
    value = NumericField(default=0)
    changed = Signal()


class BoundaryRows(ManySchema):
    value = NumericField(default=0)


@dataclass
class BoundaryStack:
    store: Store
    host: StoreHost
    client: StoreClient
    state: StateManager
    endpoint: str


@pytest_asyncio.fixture
async def boundary_stack(
    tmp_path: Path, transport_kind: TransportKind
) -> AsyncIterator[BoundaryStack]:
    tree = (
        Branch(BoundaryFirst)
        .child("second", Branch(BoundarySecond))
        .child("rows", Branch(BoundaryRows))
    )
    endpoint = (
        f"direct://{uuid4().hex}"
        if transport_kind == "direct"
        else "http://127.0.0.1:0"
    )
    config = (
        Builder()
        .tree(tree)
        .storage_path(tmp_path)
        .host(HostConfig(endpoint))
        .build()
    )
    store = Store(config)
    host = StoreHost(store)
    client: StoreClient | None = None
    try:
        store.boot()
        await host.start()
        endpoint = host.transport.endpoint.get("endpoint", host.endpoint)
        client = StoreClient(max_buffered_messages=1)
        yield BoundaryStack(
            store, host, client, StateManager(client), endpoint
        )
    finally:
        if client is not None:
            await client.disconnect()
        await host.stop()
        store.shutdown()


def assert_client_work_cleared(client: StoreClient) -> None:
    assert not client.online
    assert not client.synced
    assert client._queue.empty
    assert client._futures.empty
    assert client._subscription_requests == {}
    assert client._messages == []


@pytest.mark.asyncio
async def test_commit_stages_all_managers_before_callbacks_and_reply(
    boundary_stack: BoundaryStack,
) -> None:
    stack = boundary_stack
    other_manager = StateManager(stack.client)
    first: Any = stack.state.create(BoundaryFirst)
    second: Any = other_manager.create(BoundarySecond)
    await stack.client.connect(stack.endpoint)
    observed: list[tuple[str, int, int]] = []

    def record(label: str) -> None:
        observed.append((label, first.value, second.value))

    first.bind(value=lambda *_: record("first value"))
    second.bind(value=lambda *_: record("second value"))
    first.bind(on_changed=lambda *_: record("first signal"))
    second.bind(on_changed=lambda *_: record("second signal"))

    async with asyncio.timeout(5):
        reply = await stack.client.transaction(
            transaction_id="two-managers",
            actions=(
                SetRequestAction("BoundaryFirst", (("value", 1),)),
                SignalRequestAction("BoundarySecond", "changed"),
                SetRequestAction("BoundarySecond", (("value", 2),)),
                SignalRequestAction("BoundaryFirst", "changed"),
            ),
        )

    assert reply == TransactionResponse("two-managers")
    assert len(observed) == 4
    assert {label for label, _, _ in observed[:2]} == {
        "first value",
        "second value",
    }
    assert [label for label, _, _ in observed[2:]] == [
        "second signal",
        "first signal",
    ]
    assert all((one, two) == (1, 2) for _, one, two in observed)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["missing", "empty", "wrong_id", "wrong_type"]
)
async def test_malformed_response_settles_every_pending_request(
    boundary_stack: BoundaryStack, fault: str
) -> None:
    stack = boundary_stack
    await stack.client.connect(stack.endpoint)
    handle = stack.host.requests.handle

    def malformed(client_id: str, batch: RequestBatch) -> ResponseBatch:
        actual = handle(client_id, batch)
        assert len(actual.items) == 2
        if fault == "missing":
            return ResponseBatch(actual.items[:1])
        if fault == "empty":
            return ResponseBatch(())
        second = (
            TransactionResponse("wrong")
            if fault == "wrong_id"
            else UnsubscribeResponse("wrong-kind")
        )
        return ResponseBatch((actual.items[0], second))

    stack.host.requests.handle = malformed
    first = stack.client.transaction(
        transaction_id="first",
        actions=(SetRequestAction("BoundaryFirst", (("value", 1),)),),
    )
    second = stack.client.transaction(
        transaction_id="second",
        actions=(SetRequestAction("BoundaryFirst", (("value", 2),)),),
    )
    async with asyncio.timeout(5):
        outcomes = await asyncio.gather(first, second, return_exceptions=True)
    assert all(
        isinstance(result, StoreProtocolError) for result in outcomes
    ), outcomes
    assert_client_work_cleared(stack.client)
    assert stack.store.snapshot_by_schema("BoundaryFirst")["value"] == 2
    stack.host.requests.handle = handle
    await stack.client.connect(stack.endpoint)
    assert stack.client.synced
    assert stack.store.snapshot_by_schema("BoundaryFirst")["value"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["wrong_instance", "no_snapshot"])
async def test_bad_subscription_reply_aborts_sync_and_can_reconnect(
    boundary_stack: BoundaryStack, fault: str
) -> None:
    stack = boundary_stack
    first = stack.state.create(BoundaryFirst)
    second = stack.state.create(BoundarySecond)
    handle = stack.host.requests.handle

    def malformed(client_id: str, batch: RequestBatch) -> ResponseBatch:
        actual = handle(client_id, batch)
        reply = actual.items[1]
        assert isinstance(reply, SingleSubscriptionResponse)
        changed = (
            replace(reply, store_instance_id="different-store")
            if fault == "wrong_instance"
            else replace(reply, values=None)
        )
        return ResponseBatch((actual.items[0], changed))

    stack.host.requests.handle = malformed
    async with asyncio.timeout(5):
        with pytest.raises(StoreProtocolError):
            await stack.client.connect(stack.endpoint)
    assert_client_work_cleared(stack.client)
    assert not first.synced and not second.synced
    stack.host.requests.handle = handle
    await stack.client.connect(stack.endpoint)
    assert stack.client.synced and first.synced and second.synced


@pytest.mark.asyncio
@pytest.mark.parametrize("after_connect", [False, True])
async def test_offline_mutations_leave_no_overlays_or_pending_work(
    boundary_stack: BoundaryStack, after_connect: bool
) -> None:
    stack = boundary_stack
    state: Any = stack.state.create(BoundaryFirst)
    if after_connect:
        await stack.client.connect(stack.endpoint)
        await stack.client.disconnect()
    with pytest.raises(StoreConnectionError):
        state.set(value=2)
    with pytest.raises(StoreConnectionError):
        state.value = 3
    assert state.value == 0
    assert state.pending_count == 0
    assert stack.state._transactions == {}
    assert_client_work_cleared(stack.client)
    await stack.client.connect(stack.endpoint)
    assert state.value == 0 and state.synced


@pytest.mark.asyncio
async def test_disconnect_settles_waiter_without_replaying_accepted_request(
    boundary_stack: BoundaryStack,
) -> None:
    stack = boundary_stack
    await stack.client.connect(stack.endpoint)
    entered, release, finished = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    send_request = stack.host.request

    async def gated(client_id: str, request: RequestBatch) -> ResponseBatch:
        entered.set()
        try:
            await release.wait()
            return await send_request(client_id, request)
        finally:
            finished.set()

    stack.host.request = gated
    future = stack.client.transaction(
        transaction_id="interrupted",
        actions=(SetRequestAction("BoundaryFirst", (("value", 3),)),),
    )
    async with asyncio.timeout(5):
        await entered.wait()
        await stack.client.disconnect()
        with pytest.raises(StoreConnectionError):
            await future
        release.set()
        await finished.wait()
    assert_client_work_cleared(stack.client)
    # An accepted request may finish remotely after local disconnection.
    committed = stack.store.snapshot_by_schema("BoundaryFirst")["value"]
    revision = stack.store.get_node_by_schema("BoundaryFirst").revision
    assert (committed, revision) in {(0, 0), (3, 1)}
    stack.host.request = send_request
    state: Any = stack.state.create(BoundaryFirst)
    await stack.client.connect(stack.endpoint)
    assert stack.client.synced and state.value == committed
    assert stack.store.get_node_by_schema("BoundaryFirst").revision == revision


@pytest.mark.asyncio
async def test_wrong_store_instance_event_disconnects_before_applying_values(
    boundary_stack: BoundaryStack,
) -> None:
    stack = boundary_stack
    state: Any = stack.state.create(BoundaryFirst)
    await stack.client.connect(stack.endpoint)
    disconnected = asyncio.Event()
    on_disconnect = stack.client.on_disconnect

    def observed_disconnect(error: Exception | None = None) -> None:
        on_disconnect(error)
        disconnected.set()

    stack.client.on_disconnect = observed_disconnect
    subscription = stack.client._subscriptions.get(BoundaryFirst)
    assert (
        subscription is not None and subscription.subscription_id is not None
    )
    message = EventMessage(
        "wrong-store",
        (
            SubscriptionEvent(
                subscription.subscription_id,
                {"BoundaryFirst": 1},
                (SetEventAction((("value", 99),)),),
            ),
        ),
    )
    host_client = next(iter(stack.host.clients._clients.values()))
    host_client._prepare = lambda _client_id, event: event
    stack.host.clients.enqueue(host_client.client_id, message)
    async with asyncio.timeout(5):
        await disconnected.wait()
    assert not stack.client.online and not state.synced
    assert state.value == 0
    assert stack.host.running


@pytest.mark.asyncio
async def test_snapshot_event_buffer_overflow_disconnects_truthfully(
    boundary_stack: BoundaryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = boundary_stack
    state: Any = stack.state.create(BoundaryFirst)
    captured, release = asyncio.Event(), asyncio.Event()
    first_event, disconnected = asyncio.Event(), asyncio.Event()
    requests = intercept_client_requests(monkeypatch, stack.client)
    send_request = requests.send
    on_message, on_disconnect = (
        stack.client.on_message,
        stack.client.on_disconnect,
    )

    async def gated(request: RequestBatch) -> ResponseBatch:
        response = await send_request(request)
        captured.set()
        await release.wait()
        return response

    def observed_message(message: EventMessage) -> None:
        on_message(message)
        first_event.set()

    def observed_disconnect(error: Exception | None = None) -> None:
        on_disconnect(error)
        disconnected.set()

    requests.request = gated
    stack.client.on_message = observed_message
    stack.client.on_disconnect = observed_disconnect
    connecting = asyncio.create_task(stack.client.connect(stack.endpoint))
    try:
        async with asyncio.timeout(5):
            await captured.wait()
            stack.store.execute(
                (SetRequestAction("BoundaryFirst", (("value", 1),)),),
                transaction_id="buffer-first",
            )
            await first_event.wait()
            stack.store.execute(
                (SetRequestAction("BoundaryFirst", (("value", 2),)),),
                transaction_id="buffer-overflow",
            )
            await disconnected.wait()
            release.set()
            with pytest.raises(StoreProtocolError, match="buffer is full"):
                await connecting
        assert_client_work_cleared(stack.client)
        assert not state.synced
        requests.request = send_request
        await stack.client.connect(stack.endpoint)
        assert state.synced and state.value == 2
        assert stack.host.running
    finally:
        release.set()
        await asyncio.gather(connecting, return_exceptions=True)


@pytest.mark.asyncio
async def test_slow_client_queue_overflow_does_not_interrupt_other_clients(
    boundary_stack: BoundaryStack,
) -> None:
    stack = boundary_stack
    stack.host.clients._max_pending = 1
    slow: Any = stack.state.create(BoundaryFirst)
    await stack.client.connect(stack.endpoint)
    slow_host_client = next(iter(stack.host.clients._clients.values()))
    other = StoreClient()
    other_manager = StateManager(other)
    fast: Any = other_manager.create(BoundaryFirst)
    entered, release = asyncio.Event(), asyncio.Event()
    disconnected = asyncio.Event()
    received = {value: asyncio.Event() for value in (1, 2, 3)}
    send = slow_host_client._send
    on_disconnect = stack.client.on_disconnect

    async def blocked(client_id: str, message: EventMessage) -> None:
        entered.set()
        await release.wait()
        await send(client_id, message)

    def observed_disconnect(error: Exception | None = None) -> None:
        on_disconnect(error)
        disconnected.set()

    slow_host_client._send = blocked
    stack.client.on_disconnect = observed_disconnect
    fast.bind(value=lambda _state, value: received[value].set())
    try:
        await other.connect(stack.endpoint)
        async with asyncio.timeout(5):
            for value in (1, 2, 3):
                stack.store.execute(
                    (SetRequestAction("BoundaryFirst", (("value", value),)),),
                    transaction_id=f"overflow-{value}",
                )
                await received[value].wait()
                if value == 1:
                    await entered.wait()
            await disconnected.wait()
        assert stack.host.running and other.online and fast.synced
        assert fast.value == 3
        assert not slow.synced
        assert not stack.client.online
    finally:
        release.set()
        await other.disconnect()


@pytest.mark.asyncio
async def test_client_delivery_callback_failure_is_isolated_to_that_client(
    boundary_stack: BoundaryStack,
) -> None:
    stack = boundary_stack
    broken: Any = stack.state.create(BoundaryFirst)
    await stack.client.connect(stack.endpoint)
    other = StoreClient()
    other_manager = StateManager(other)
    healthy: Any = other_manager.create(BoundaryFirst)
    delivered, disconnected = asyncio.Event(), asyncio.Event()
    on_disconnect = stack.client.on_disconnect

    def fail_delivery(message: EventMessage) -> None:
        del message
        raise RuntimeError("Injected manager delivery failure")

    def observed_disconnect(error: Exception | None = None) -> None:
        on_disconnect(error)
        disconnected.set()

    stack.client.on_message = fail_delivery
    stack.client.on_disconnect = observed_disconnect
    healthy.bind(value=lambda *_: delivered.set())
    try:
        await other.connect(stack.endpoint)
        stack.store.execute(
            (SetRequestAction("BoundaryFirst", (("value", 1),)),),
            transaction_id="callback-failure",
        )
        async with asyncio.timeout(5):
            await delivered.wait()
            await disconnected.wait()
        assert healthy.value == 1 and healthy.synced
        assert not broken.synced and not stack.client.online
        assert stack.host.running and other.online
        operation = healthy.set(value=2)
        await operation
        assert operation.success and healthy.value == 2
    finally:
        await other.disconnect()


@pytest.mark.asyncio
async def test_delivery_barrier_does_not_consume_message_capacity(
    boundary_stack: BoundaryStack,
) -> None:
    stack = boundary_stack
    stack.host.clients._max_pending = 1
    state: Any = stack.state.create(BoundaryFirst)
    await stack.client.connect(stack.endpoint)
    operation = state.set(value=1)
    async with asyncio.timeout(5):
        await operation
    assert operation.success
    assert stack.client.online and state.synced and state.value == 1


@pytest.mark.asyncio
async def test_request_timeout_is_preserved_and_clears_pending_work(
    boundary_stack: BoundaryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    from socketio.exceptions import TimeoutError as SocketIOTimeoutError

    stack = boundary_stack
    await stack.client.connect(stack.endpoint)
    transport = stack.client._require_transport()
    calls = 0

    async def timeout(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if isinstance(transport, SocketIOClientTransport):
            raise SocketIOTimeoutError("Injected Socket.IO timeout")
        raise StoreTimeoutError("Injected Direct request timeout")

    if isinstance(transport, SocketIOClientTransport):
        monkeypatch.setattr(transport.sio, "call", timeout)
    else:
        monkeypatch.setattr(transport, "request", timeout)
    first = stack.client.transaction(
        transaction_id="timeout-first",
        actions=(SetRequestAction("BoundaryFirst", (("value", 1),)),),
    )
    second = stack.client.transaction(
        transaction_id="timeout-second",
        actions=(SetRequestAction("BoundaryFirst", (("value", 2),)),),
    )
    async with asyncio.timeout(5):
        results = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(result, StoreTimeoutError) for result in results)
    assert calls == 1
    assert_client_work_cleared(stack.client)
    assert stack.store.snapshot_by_schema("BoundaryFirst")["value"] == 0
    assert stack.host.running


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "empty",
    [
        SetRequestAction("BoundaryFirst", ()),
        SetRequestAction("BoundaryRows", ()),
        SetRowRequestAction("BoundaryRows", "missing", ()),
        SetRowRequestAction("BoundaryFirst", "wrong-kind", ()),
        SetRowRequestAction("BoundaryRows", "existing", ()),
    ],
)
async def test_empty_set_actions_are_rejected_without_committing_prior_changes(
    boundary_stack: BoundaryStack,
    empty: SetRequestAction | SetRowRequestAction,
) -> None:
    stack = boundary_stack
    await stack.client.connect(stack.endpoint)
    seeded = await stack.client.transaction(
        transaction_id="default-row",
        actions=(InsertRequestAction("BoundaryRows", "existing", ()),),
    )
    assert isinstance(seeded, TransactionResponse)
    direct_result = stack.store.execute(
        (SetRequestAction("BoundaryFirst", (("value", 5),)), empty),
        transaction_id="direct-empty-set",
    )
    assert not direct_result.success
    assert isinstance(direct_result.error, InvalidOperationError)
    response = await stack.client.transaction(
        transaction_id="empty-set",
        actions=(SetRequestAction("BoundaryFirst", (("value", 5),)), empty),
    )
    assert isinstance(response, TransactionFailureResponse)
    assert response.failure.code == "invalid_operation"
    assert stack.store.snapshot_by_schema("BoundaryFirst")["value"] == 0
    assert stack.store.get_node_by_schema("BoundaryFirst").revision == 0
    assert stack.client.online
    # Empty transactions and same-value changes remain valid no-op requests.
    empty_transaction = await stack.client.transaction(
        transaction_id="empty-transaction", actions=()
    )
    unchanged = await stack.client.transaction(
        transaction_id="unchanged-set",
        actions=(SetRequestAction("BoundaryFirst", (("value", 0),)),),
    )
    assert isinstance(empty_transaction, TransactionResponse)
    assert isinstance(unchanged, TransactionResponse)
    assert stack.store.get_node_by_schema("BoundaryFirst").revision == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["wire_revision", "revision_gap", "instance"]
)
async def test_bad_incoming_event_preserves_protocol_cause_for_pending_work(
    boundary_stack: BoundaryStack, fault: str
) -> None:
    stack = boundary_stack
    state: Any = stack.state.create(BoundaryFirst)
    await stack.client.connect(stack.endpoint)
    entered, release, finished = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    send_request = stack.host.request

    async def gated(client_id: str, request: RequestBatch) -> ResponseBatch:
        entered.set()
        try:
            await release.wait()
            return await send_request(client_id, request)
        finally:
            finished.set()

    stack.host.request = gated
    first = stack.client.transaction(
        transaction_id="malformed-event-first",
        actions=(SetRequestAction("BoundaryFirst", (("value", 1),)),),
    )
    second = stack.client.transaction(
        transaction_id="malformed-event-second",
        actions=(SetRequestAction("BoundaryFirst", (("value", 2),)),),
    )
    subscription = stack.client._subscriptions.get(BoundaryFirst)
    assert (
        subscription is not None and subscription.subscription_id is not None
    )
    revision = -1 if fault == "wire_revision" else 2
    instance = (
        "different-store" if fault == "instance" else stack.store.instance_id
    )
    message = EventMessage(
        instance,
        (
            SubscriptionEvent(
                subscription.subscription_id,
                {"BoundaryFirst": revision},
                (SetEventAction((("value", 99),)),),
            ),
        ),
    )
    host_client = next(iter(stack.host.clients._clients.values()))
    host_client._prepare = lambda _client_id, event: event
    try:
        async with asyncio.timeout(5):
            await entered.wait()
            stack.host.clients.enqueue(host_client.client_id, message)
            results = await asyncio.gather(
                first, second, return_exceptions=True
            )
            assert all(
                isinstance(result, StoreProtocolError) for result in results
            ), results
            assert_client_work_cleared(stack.client)
            assert state.value == 0 and not state.synced
            assert stack.host.running
    finally:
        release.set()
        async with asyncio.timeout(5):
            await finished.wait()
