from __future__ import annotations

import asyncio
from typing import Any

import pytest

from kivyfn.store.query import Query
from kivyfn.store.client.subscription import SubscriptionSource
from kivyfn.store.error import InvalidOperationError, StoreSubscriptionError
from kivyfn.store.protocol.message.requests import (
    CollectionSubscriptionRequest,
    InsertRequestAction,
    QueryRequest,
    QuerySubscriptionRequest,
    RequestBatch,
    RequestItem,
    SetRequestAction,
    SingleSubscriptionRequest,
    TransactionRequest,
    UnsubscribeRequest,
)
from kivyfn.store.protocol.message.responses import (
    FailureResponse,
    QueryResponse,
    ResponseBatch,
    TransactionResponse,
)
from kivyfn.store.schema import ManySchema, NumericField, SingleSchema
from tests.client.test_failure_boundaries import (
    BoundaryFirst,
    BoundaryRows,
    BoundarySecond,
    BoundaryStack,
)
from tests.client.test_failure_boundaries import (
    boundary_stack as boundary_stack,
)
from tests.helpers import intercept_client_requests


class MissingSingle(SingleSchema):
    value = NumericField(default=0)


class MissingRows(ManySchema):
    value = NumericField(default=0)


def _schema_name(request: RequestItem) -> str | None:
    if isinstance(
        request, (SingleSubscriptionRequest, CollectionSubscriptionRequest)
    ):
        return request.schema
    if isinstance(request, QuerySubscriptionRequest):
        return request.query.schema_name
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.parametrize(
    "source", [MissingSingle, MissingRows, Query(MissingRows)]
)
async def test_unknown_subscription_keeps_healthy_state_connected(
    boundary_stack: BoundaryStack,
    source: SubscriptionSource,
    incremental: bool,
) -> None:
    stack = boundary_stack
    healthy: Any = stack.state.create(BoundaryFirst)
    if incremental:
        await stack.client.connect(stack.endpoint)
    failed = stack.state.create(source)
    with pytest.raises(StoreSubscriptionError) as caught:
        await stack.client.connect(stack.endpoint)

    transport = stack.client._require_transport()
    host_clients = tuple(stack.host.clients._clients)
    subscription = stack.client._subscriptions.get(source)
    assert subscription is not None
    assert subscription.failure == caught.value.failure
    assert subscription.failure is not None
    assert subscription.failure.code == "node_not_found"
    assert not subscription.active
    assert stack.client.online and not stack.client.synced
    assert healthy.synced and not failed.synced
    assert not stack.client._subscription_requests
    assert not stack.client._resubscribing

    for value in (1, 2, 3):
        changed = await healthy.set(value=value)
        assert changed.success
        assert healthy.value == value and healthy.synced
        assert stack.client.online and not stack.client.synced
        assert stack.client._messages == []

    assert stack.client._require_transport() is transport
    assert tuple(stack.host.clients._clients) == host_clients
    assert len(stack.host.subscriptions) == 1
    stack.state.release(source)
    assert stack.client.synced
    await stack.client.connect()
    assert stack.client.synced and healthy.synced
    assert stack.client._require_transport() is transport
    assert tuple(stack.host.clients._clients) == host_clients


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source", [BoundarySecond, BoundaryRows, Query(BoundaryRows)]
)
async def test_connect_retries_only_failed_subscription_without_reconnecting(
    boundary_stack: BoundaryStack, source: SubscriptionSource
) -> None:
    stack = boundary_stack
    healthy: Any = stack.state.create(BoundaryFirst)
    retrying: Any = stack.state.create(source)
    handle = stack.host.requests.handle
    refused = InvalidOperationError("Subscription temporarily refused")
    target_name = (
        source.schema_name if isinstance(source, Query) else source.__name__
    )
    captured: list[RequestItem] = []
    reject = True

    def refuse_once(client_id: str, batch: RequestBatch) -> ResponseBatch:
        nonlocal reject
        replies = []
        for item in batch.items:
            captured.append(item)
            if reject and _schema_name(item) == target_name:
                reject = False
                replies.append(FailureResponse(refused.failure()))
            else:
                replies.extend(handle(client_id, RequestBatch((item,))).items)
        return ResponseBatch(tuple(replies))

    stack.host.requests.handle = refuse_once
    with pytest.raises(StoreSubscriptionError) as caught:
        await stack.client.connect(stack.endpoint)
    assert caught.value.failure == refused.failure()
    assert healthy.synced and not retrying.synced
    healthy_subscription = stack.client._subscriptions.get(BoundaryFirst)
    failed_subscription = stack.client._subscriptions.get(source)
    assert healthy_subscription is not None and failed_subscription is not None
    healthy_id = healthy_subscription.subscription_id
    transport = stack.client._require_transport()
    host_clients = tuple(stack.host.clients._clients)
    actions = (
        (SetRequestAction("BoundarySecond", (("value", 42),)),)
        if source is BoundarySecond
        else (InsertRequestAction("BoundaryRows", "new", (("value", 42),)),)
    )
    await stack.client.transaction(
        transaction_id="before-retry", actions=actions
    )

    await stack.client.connect()
    assert stack.client.online and stack.client.synced
    assert healthy.synced and retrying.synced
    assert failed_subscription.failure is None
    assert failed_subscription.active
    assert healthy_subscription.subscription_id == healthy_id
    assert stack.client._require_transport() is transport
    assert tuple(stack.host.clients._clients) == host_clients
    assert len(stack.host.subscriptions) == 2
    assert sum(_schema_name(item) == "BoundaryFirst" for item in captured) == 1
    assert sum(_schema_name(item) == target_name for item in captured) == 2
    if source is BoundarySecond:
        assert retrying.value == 42
    else:
        assert retrying["new"].get("value") == 42
    count = len(captured)
    await stack.client.connect()
    assert len(captured) == count


@pytest.mark.asyncio
async def test_subscription_failure_does_not_discard_batched_mutation_or_query(
    boundary_stack: BoundaryStack,
) -> None:
    stack = boundary_stack
    healthy: Any = stack.state.create(BoundaryFirst)
    await stack.client.connect(stack.endpoint)
    handle = stack.host.requests.handle
    batches: list[RequestBatch] = []

    def capture(client_id: str, batch: RequestBatch) -> ResponseBatch:
        batches.append(batch)
        return handle(client_id, batch)

    stack.host.requests.handle = capture
    failed = stack.state.create(MissingSingle)
    mutation = healthy.set(value=17)
    query = stack.client.query(Query(BoundaryRows))
    async with asyncio.timeout(5):
        with pytest.raises(StoreSubscriptionError):
            await stack.client.connect()
        response = await query
        await mutation
    assert len(batches) == 1
    assert tuple(type(item) for item in batches[0].items) == (
        SingleSubscriptionRequest,
        TransactionRequest,
        QueryRequest,
    )
    assert isinstance(response, QueryResponse) and response.rows == ()
    assert mutation.success and healthy.pending_count == 0
    assert healthy.synced and healthy.value == 17
    assert not failed.synced
    assert stack.client.online and not stack.client.synced
    assert stack.client._futures.empty


@pytest.mark.asyncio
async def test_unsubscribe_failure_keeps_connection_and_other_events_usable(
    boundary_stack: BoundaryStack,
) -> None:
    stack = boundary_stack
    healthy: Any = stack.state.create(BoundaryFirst)
    removed: Any = stack.state.create(BoundarySecond)
    await stack.client.connect(stack.endpoint)
    transport = stack.client._require_transport()
    host_clients = tuple(stack.host.clients._clients)
    handle = stack.host.requests.handle
    rejected: list[str] = []

    def refuse_unsubscribe(
        client_id: str, batch: RequestBatch
    ) -> ResponseBatch:
        replies = []
        for item in batch.items:
            if isinstance(item, UnsubscribeRequest):
                rejected.append(item.subscription_id)
                error = InvalidOperationError(
                    "Unsubscribe temporarily refused"
                )
                replies.append(FailureResponse(error.failure()))
            else:
                replies.extend(handle(client_id, RequestBatch((item,))).items)
        return ResponseBatch(tuple(replies))

    stack.host.requests.handle = refuse_unsubscribe
    stack.state.release(BoundarySecond)
    result = await stack.client.transaction(
        transaction_id="after-unsubscribe-refusal",
        actions=(
            SetRequestAction("BoundaryFirst", (("value", 3),)),
            SetRequestAction("BoundarySecond", (("value", 9),)),
        ),
    )
    assert isinstance(result, TransactionResponse)
    assert len(rejected) == 1
    assert stack.client.online and stack.client.synced
    assert healthy.synced and healthy.value == 3
    assert not removed.synced and removed.value == 0
    assert stack.client._require_transport() is transport
    assert tuple(stack.host.clients._clients) == host_clients
    await stack.client.connect()
    await healthy.set(value=4)
    assert healthy.synced and healthy.value == 4


@pytest.mark.asyncio
async def test_stale_failure_does_not_poison_recreated_subscription(
    boundary_stack: BoundaryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = boundary_stack
    healthy = stack.state.create(BoundaryFirst)
    original = stack.state.create(BoundarySecond)
    handle = stack.host.requests.handle
    first_reply, release_first = asyncio.Event(), asyncio.Event()
    next_request, release_next = asyncio.Event(), asyncio.Event()
    refuse = True

    def refuse_once(client_id: str, batch: RequestBatch) -> ResponseBatch:
        nonlocal refuse
        replies = []
        for item in batch.items:
            if refuse and _schema_name(item) == "BoundarySecond":
                refuse = False
                error = InvalidOperationError("Obsolete subscription refusal")
                replies.append(FailureResponse(error.failure()))
            else:
                replies.extend(handle(client_id, RequestBatch((item,))).items)
        return ResponseBatch(tuple(replies))

    stack.host.requests.handle = refuse_once
    requests = intercept_client_requests(monkeypatch, stack.client)
    calls = 0

    async def gated(request: RequestBatch) -> ResponseBatch:
        nonlocal calls
        calls += 1
        if calls == 1:
            response = await requests.send(request)
            first_reply.set()
            await release_first.wait()
            return response
        next_request.set()
        await release_next.wait()
        return await requests.send(request)

    requests.request = gated
    connecting = asyncio.create_task(stack.client.connect(stack.endpoint))
    try:
        async with asyncio.timeout(5):
            await first_reply.wait()
            old_subscription = stack.client._subscriptions.get(BoundarySecond)
            stack.state.release(BoundarySecond)
            recreated = stack.state.create(BoundarySecond)
            current = stack.client._subscriptions.get(BoundarySecond)
            assert current is not None and current is not old_subscription
            release_first.set()
            await next_request.wait()
            assert current.failure is None
            assert stack.client.online
            assert not connecting.done()
            release_next.set()
            await connecting
        assert recreated is original
        assert healthy.synced and recreated.synced and stack.client.synced
        assert current.failure is None and current.active
        assert len(stack.host.subscriptions) == 2
    finally:
        release_first.set()
        release_next.set()
        await asyncio.gather(connecting, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_disconnect_failed_subscription(
    boundary_stack: BoundaryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = boundary_stack
    healthy = stack.state.create(BoundaryFirst)
    stack.state.create(MissingSingle)
    captured, release, completed = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    requests = intercept_client_requests(monkeypatch, stack.client)

    async def gated(request: RequestBatch) -> ResponseBatch:
        response = await requests.send(request)
        captured.set()
        await release.wait()
        return response

    requests.request = gated
    connecting = asyncio.create_task(stack.client.connect(stack.endpoint))
    try:
        async with asyncio.timeout(5):
            await captured.wait()
            background = stack.client._connect_task
            assert background is not None
            background.add_done_callback(lambda _: completed.set())
            connecting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await connecting
            release.set()
            await completed.wait()
        assert isinstance(background.exception(), StoreSubscriptionError)
        assert stack.client.online and healthy.synced
        assert not stack.client.synced
        stack.state.release(MissingSingle)
        await stack.client.connect()
        assert stack.client.synced and healthy.synced
    finally:
        release.set()
        await asyncio.gather(connecting, return_exceptions=True)


@pytest.mark.asyncio
async def test_all_subscriptions_failed_still_allows_new_healthy_subscription(
    boundary_stack: BoundaryStack,
) -> None:
    stack = boundary_stack
    failed = stack.state.create(MissingSingle)
    with pytest.raises(StoreSubscriptionError):
        await stack.client.connect(stack.endpoint)
    assert stack.client.online and not failed.synced
    assert len(stack.host.subscriptions) == 0
    assert stack.client._connection_instance_id is None
    healthy = stack.state.create(BoundaryFirst)
    with pytest.raises(StoreSubscriptionError):
        await stack.client.connect()
    assert stack.client.online and healthy.synced
    assert not failed.synced and not stack.client.synced
    assert stack.client._connection_instance_id == stack.store.instance_id
    result = await stack.client.transaction(
        transaction_id="after-all-failed",
        actions=(SetRequestAction("BoundaryFirst", (("value", 7),)),),
    )
    assert isinstance(result, TransactionResponse)
    assert healthy.get("value") == 7


@pytest.mark.asyncio
async def test_failure_waits_for_subscription_created_by_synced_callback(
    boundary_stack: BoundaryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = boundary_stack
    healthy = stack.state.create(BoundaryFirst)
    stack.state.create(MissingSingle)
    created: list[Any] = []
    pending, release = asyncio.Event(), asyncio.Event()
    requests = intercept_client_requests(monkeypatch, stack.client)

    def create_when_synced(_state: Any, synced: bool) -> None:
        if synced and not created:
            created.append(stack.state.create(BoundarySecond))

    async def gated(request: RequestBatch) -> ResponseBatch:
        if any(
            _schema_name(item) == "BoundarySecond" for item in request.items
        ):
            pending.set()
            await release.wait()
        return await requests.send(request)

    healthy.bind(synced=create_when_synced)
    requests.request = gated
    connecting = asyncio.create_task(stack.client.connect(stack.endpoint))
    try:
        async with asyncio.timeout(5):
            await pending.wait()
            assert len(created) == 1 and not created[0].synced
            assert stack.client._subscription_requests
            sync = stack.client._sync_future
            assert sync is not None and not sync.done()
            release.set()
            with pytest.raises(StoreSubscriptionError):
                await connecting
        assert healthy.synced and created[0].synced
        assert stack.client.online and not stack.client.synced
    finally:
        release.set()
        await asyncio.gather(connecting, return_exceptions=True)
