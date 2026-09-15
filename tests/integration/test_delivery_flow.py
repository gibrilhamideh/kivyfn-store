from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio

from kivyfn.store import Store, StoreClient, StoreHost
from kivyfn.store.builder import Branch, Builder
from kivyfn.store.error import StoreConnectionError
from kivyfn.store.protocol.message.event import EventMessage
from kivyfn.store.protocol.message.requests import (
    RequestBatch,
    SetRequestAction,
    SignalRequestAction,
)
from kivyfn.store.protocol.message.responses import (
    ResponseBatch,
    TransactionFailureResponse,
    TransactionResponse,
)
from kivyfn.store.schema import SingleSchema
from kivyfn.store.schema.fields import NumericField
from kivyfn.store.schema.signal import Signal
from kivyfn.store.state.manager import StateManager
from tests.conftest import TransportKind
from tests.helpers import intercept_client_requests


class DeliveryFirst(SingleSchema):
    value = NumericField(default=0)
    ping = Signal()


class DeliverySecond(SingleSchema):
    value = NumericField(default=0)
    ping = Signal()


@dataclass
class DeliveryStack:
    store: Store
    host: StoreHost
    client: StoreClient
    state: StateManager
    endpoint: str


@pytest_asyncio.fixture
async def delivery_stack(
    tmp_path: Path, transport_kind: TransportKind
) -> AsyncIterator[DeliveryStack]:
    tree = Branch(DeliveryFirst).child("second", Branch(DeliverySecond))
    root, config = Builder().tree(tree).storage_path(tmp_path).build()
    store = Store(root, config)
    host = StoreHost(
        store,
        f"direct://{uuid4().hex}"
        if transport_kind == "direct"
        else "http://127.0.0.1:0",
    )
    store.boot()
    await host.start()
    endpoint = host.transport.endpoint.get("endpoint", host.endpoint)
    client = StoreClient()
    state = StateManager(client)
    try:
        yield DeliveryStack(store, host, client, state, endpoint)
    finally:
        await client.disconnect()
        await host.stop()
        store.shutdown()


@pytest.mark.asyncio
async def test_acknowledgement_waits_for_queued_canonical_event(
    delivery_stack: DeliveryStack,
) -> None:
    stack = delivery_stack
    reached, release = asyncio.Event(), asyncio.Event()
    send = stack.host.clients._send

    async def delayed(client_id: str, message: EventMessage) -> None:
        reached.set()
        await release.wait()
        await send(client_id, message)

    stack.host.clients._send = delayed
    first: Any = stack.state.create(DeliveryFirst)
    await stack.client.connect(stack.endpoint)
    operation = first.set(value=1)
    await reached.wait()
    assert first.value == 0
    assert not operation.done
    assert stack.store.snapshot_by_schema("DeliveryFirst")["value"] == 1
    release.set()
    await operation
    assert operation.success
    assert first.value == 1
    assert first.pending_count == 0


@pytest.mark.asyncio
async def test_cross_schema_signals_and_bindings_see_final_commit(
    delivery_stack: DeliveryStack,
) -> None:
    stack = delivery_stack
    first: Any = stack.state.create(DeliveryFirst)
    second: Any = stack.state.create(DeliverySecond)
    await stack.client.connect(stack.endpoint)
    observations: list[tuple[str, int, int]] = []
    first.bind(
        on_ping=lambda *_: observations.append(
            ("first", first.value, second.value)
        )
    )
    second.bind(
        on_ping=lambda *_: observations.append(
            ("second", first.value, second.value)
        )
    )
    first.bind(
        value=lambda *_: observations.append(
            ("value", first.value, second.value)
        )
    )
    with stack.state.transaction() as transaction:
        first.ping()
        first.set(value=1)
        second.set(value=2)
        second.ping()
    await transaction
    assert transaction.success
    assert {row[0] for row in observations} == {"value", "first", "second"}
    assert all(row[1:] == (1, 2) for row in observations)


@pytest.mark.asyncio
async def test_strict_noop_completes_without_event(
    delivery_stack: DeliveryStack,
) -> None:
    first: Any = delivery_stack.state.create(DeliveryFirst)
    await delivery_stack.client.connect(delivery_stack.endpoint)
    commits = []
    delivery_stack.store.subscribe_commits(commits.append)
    operation = first.set(value=0)
    await operation
    assert operation.success and not commits
    assert first.value == 0 and first.pending_count == 0


@pytest.mark.asyncio
async def test_incremental_snapshot_handoff_keeps_early_events(
    delivery_stack: DeliveryStack,
) -> None:
    stack = delivery_stack
    await stack.client.connect(stack.endpoint)
    captured, release = asyncio.Event(), asyncio.Event()
    original_request = stack.client._require_transport().request

    async def delayed(request: RequestBatch) -> ResponseBatch:
        response = await original_request(request)
        captured.set()
        await release.wait()
        return response

    stack.client._require_transport().request = delayed
    first: Any = stack.state.create(DeliveryFirst)
    await captured.wait()
    delivered = asyncio.Event()
    on_message = stack.client.on_message

    def observed(message: EventMessage) -> None:
        on_message(message)
        delivered.set()

    stack.client.on_message = observed
    stack.store.execute(
        (SetRequestAction("DeliveryFirst", (("value", 3),)),),
        transaction_id="during-subscribe",
    )
    await delivered.wait()
    release.set()
    await stack.client.connect(stack.endpoint)
    assert first.synced and first.value == 3
    subscription = stack.client._subscriptions.get(DeliveryFirst)
    assert subscription is not None
    assert subscription.revisions == {"DeliveryFirst": 1}


@pytest.mark.asyncio
async def test_subscription_added_during_initial_sync_completes(
    delivery_stack: DeliveryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = delivery_stack
    first = stack.state.create(DeliveryFirst)
    started, release = asyncio.Event(), asyncio.Event()
    requests = intercept_client_requests(monkeypatch, stack.client)
    original_request = requests.send
    first_batch = True

    async def gated(request: RequestBatch) -> ResponseBatch:
        nonlocal first_batch
        if first_batch:
            first_batch = False
            started.set()
            await release.wait()
        return await original_request(request)

    requests.request = gated
    connecting = asyncio.create_task(stack.client.connect(stack.endpoint))
    await started.wait()
    second = stack.state.create(DeliverySecond)
    release.set()
    await connecting
    assert first.synced and second.synced and stack.client.synced


@pytest.mark.asyncio
async def test_lost_reply_is_unknown_outcome_without_automatic_replay(
    delivery_stack: DeliveryStack,
) -> None:
    stack = delivery_stack
    first: Any = stack.state.create(DeliveryFirst)
    await stack.client.connect(stack.endpoint)
    commits = []
    stack.store.subscribe_commits(commits.append)
    original_request = stack.client._require_transport().request

    async def lost_reply(request: RequestBatch) -> ResponseBatch:
        await original_request(request)
        raise StoreConnectionError("Injected lost acknowledgement")

    stack.client._require_transport().request = lost_reply
    operation = first.ping()
    await operation
    assert operation.failed and not stack.client.online
    assert sum(len(commit.signals) for commit in commits) == 1
    stack.client._require_transport().request = original_request
    await stack.client.connect(stack.endpoint)
    assert first.synced
    assert sum(len(commit.signals) for commit in commits) == 1


@pytest.mark.asyncio
async def test_duplicate_transaction_id_reuses_result_not_occurrence(
    delivery_stack: DeliveryStack,
) -> None:
    stack = delivery_stack
    await stack.client.connect(stack.endpoint)
    commits = []
    stack.store.subscribe_commits(commits.append)
    action = SignalRequestAction("DeliveryFirst", "ping", {"value": [1]})
    first = await stack.client.transaction(
        transaction_id="same", actions=(action,)
    )
    repeated = await stack.client.transaction(
        transaction_id="same", actions=(action,)
    )
    conflict = await stack.client.transaction(
        transaction_id="same",
        actions=(SignalRequestAction("DeliveryFirst", "ping", "changed"),),
    )
    assert isinstance(first, TransactionResponse)
    assert first == repeated
    assert isinstance(conflict, TransactionFailureResponse)
    assert sum(len(commit.signals) for commit in commits) == 1


@pytest.mark.asyncio
async def test_permanent_transport_failure_settles_once(
    delivery_stack: DeliveryStack,
) -> None:
    stack = delivery_stack
    first: Any = stack.state.create(DeliveryFirst)
    await stack.client.connect(stack.endpoint)
    calls = 0

    async def broken(request: RequestBatch) -> ResponseBatch:
        nonlocal calls
        calls += 1
        raise ValueError("Injected encoding failure")

    stack.client._require_transport().request = broken
    operation = first.set(value=2)
    await operation
    assert calls == 1 and operation.failed
    assert first.value == 0 and first.pending_count == 0
    assert not stack.client.online


@pytest.mark.asyncio
async def test_cancelled_connect_waiter_does_not_cancel_shared_connect(
    delivery_stack: DeliveryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = delivery_stack
    first = stack.state.create(DeliveryFirst)
    started, release = asyncio.Event(), asyncio.Event()
    requests = intercept_client_requests(monkeypatch, stack.client)
    original_request = requests.send

    async def delayed(request: RequestBatch) -> ResponseBatch:
        started.set()
        await release.wait()
        return await original_request(request)

    requests.request = delayed
    cancelled = asyncio.create_task(stack.client.connect(stack.endpoint))
    await started.wait()
    surviving = asyncio.create_task(stack.client.connect(stack.endpoint))
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    release.set()
    await surviving
    assert first.synced and stack.client.online
