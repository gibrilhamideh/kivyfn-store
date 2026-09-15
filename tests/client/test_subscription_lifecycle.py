from __future__ import annotations

import asyncio
from typing import Any

import pytest

from kivyfn.store.error import StoreConnectionError
from kivyfn.store.protocol.message.event import EventMessage
from kivyfn.store.protocol.message.requests import (
    RequestBatch,
    SetRequestAction,
)
from kivyfn.store.protocol.message.responses import ResponseBatch
from tests.client.test_failure_boundaries import (
    BoundaryFirst,
    BoundarySecond,
    BoundaryStack,
)
from tests.client.test_failure_boundaries import (
    boundary_stack as boundary_stack,
)
from tests.helpers import intercept_client_requests


@pytest.mark.asyncio
async def test_cancelled_only_connect_waiter_allows_later_incremental_sync(
    boundary_stack: BoundaryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = boundary_stack
    first = stack.state.create(BoundaryFirst)
    requests = intercept_client_requests(monkeypatch, stack.client)
    original_request = requests.send
    first_captured, first_release = asyncio.Event(), asyncio.Event()
    second_started, second_release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def gated(request: RequestBatch) -> ResponseBatch:
        nonlocal calls
        calls += 1
        if calls == 1:
            response = await original_request(request)
            first_captured.set()
            await first_release.wait()
            return response
        second_started.set()
        await second_release.wait()
        return await original_request(request)

    requests.request = gated
    initial = asyncio.create_task(stack.client.connect(stack.endpoint))
    later: asyncio.Task[None] | None = None
    try:
        async with asyncio.timeout(2):
            await first_captured.wait()
            background = stack.client._connect_task
            assert background is not None
            initial.cancel()
            with pytest.raises(asyncio.CancelledError):
                await initial
            first_release.set()
            await asyncio.shield(background)
            assert first.synced
            second = stack.state.create(BoundarySecond)
            later = asyncio.create_task(stack.client.connect(stack.endpoint))
            await second_started.wait()
            assert not later.done()
            assert not second.synced
            second_release.set()
            await later
            assert second.synced and stack.client.synced
    finally:
        first_release.set()
        second_release.set()
        await asyncio.gather(initial, return_exceptions=True)
        if later is not None:
            await asyncio.gather(later, return_exceptions=True)


@pytest.mark.asyncio
async def test_unsubscribe_before_snapshot_reply_releases_host_registration(
    boundary_stack: BoundaryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = boundary_stack
    first = stack.state.create(BoundaryFirst)
    captured, release = asyncio.Event(), asyncio.Event()
    requests = intercept_client_requests(monkeypatch, stack.client)
    original_request = requests.send
    delayed = True

    async def gated(request: RequestBatch) -> ResponseBatch:
        nonlocal delayed
        response = await original_request(request)
        if delayed:
            delayed = False
            captured.set()
            await release.wait()
        return response

    requests.request = gated
    connecting = asyncio.create_task(stack.client.connect(stack.endpoint))
    try:
        async with asyncio.timeout(2):
            await captured.wait()
            assert len(stack.host.subscriptions) == 1
            stack.state.release(BoundaryFirst)
            release.set()
            await connecting
            # This request follows the unsubscribe queued while binding reply.
            await stack.client.transaction(
                transaction_id="after-unsubscribe",
                actions=(SetRequestAction("BoundarySecond", (("value", 0),)),),
            )
        assert len(stack.host.subscriptions) == 0
        assert len(stack.client._subscriptions) == 0
        assert not first.synced and stack.client.synced
    finally:
        release.set()
        await asyncio.gather(connecting, return_exceptions=True)


@pytest.mark.asyncio
async def test_release_recreate_during_snapshot_keeps_event_and_one_binding(
    boundary_stack: BoundaryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = boundary_stack
    first: Any = stack.state.create(BoundaryFirst)
    captured, release, delivered = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    requests = intercept_client_requests(monkeypatch, stack.client)
    original_request = requests.send
    on_message = stack.client.on_message
    delay = True

    async def gated(request: RequestBatch) -> ResponseBatch:
        nonlocal delay
        response = await original_request(request)
        if delay:
            delay = False
            captured.set()
            await release.wait()
        return response

    def observe(message: EventMessage) -> None:
        on_message(message)
        delivered.set()

    requests.request = gated
    stack.client.on_message = observe
    connecting = asyncio.create_task(stack.client.connect(stack.endpoint))
    try:
        async with asyncio.timeout(2):
            await captured.wait()
            stack.state.release(BoundaryFirst)
            recreated: Any = stack.state.create(BoundaryFirst)
            committed = stack.store.execute(
                (SetRequestAction("BoundaryFirst", (("value", 9),)),),
                transaction_id="recreated-before-snapshot",
            )
            assert committed.success
            await delivered.wait()
            release.set()
            await connecting
        assert recreated is first
        assert recreated.value == 9 and recreated.synced
        assert stack.client.synced
        assert len(stack.host.subscriptions) == 1
        assert len(stack.client._subscriptions) == 1
        subscription = stack.client._subscriptions.get(BoundaryFirst)
        assert subscription is not None
        assert subscription.revisions == {"BoundaryFirst": 1}
    finally:
        release.set()
        await asyncio.gather(connecting, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_only_waiter_observes_background_connect_failure(
    boundary_stack: BoundaryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = boundary_stack
    stack.state.create(BoundaryFirst)
    entered, release = asyncio.Event(), asyncio.Event()
    requests = intercept_client_requests(monkeypatch, stack.client)
    original_request = requests.send

    async def fail(request: RequestBatch) -> ResponseBatch:
        entered.set()
        await release.wait()
        raise StoreConnectionError("injected background connection failure")

    requests.request = fail
    initial = asyncio.create_task(stack.client.connect(stack.endpoint))
    background_done = asyncio.Event()
    try:
        async with asyncio.timeout(2):
            await entered.wait()
            background = stack.client._connect_task
            assert background is not None
            background.add_done_callback(lambda _: background_done.set())
            initial.cancel()
            with pytest.raises(asyncio.CancelledError):
                await initial
            release.set()
            await background_done.wait()
        assert not stack.client.online
        assert stack.client._connect_task is None
        assert not background._log_traceback
        requests.request = original_request
        await stack.client.connect(stack.endpoint)
        assert stack.client.synced
    finally:
        release.set()
        await asyncio.gather(initial, return_exceptions=True)
