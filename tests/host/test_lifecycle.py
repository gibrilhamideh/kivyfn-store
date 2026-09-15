from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from kivyfn.store import Store, StoreClient, StoreHost
from kivyfn.store.builder.config import HostConfig
from kivyfn.store.error import StoreInternalError, StoreStateError
from kivyfn.store.protocol.message.requests import InsertRequestAction
from tests.conftest import StateStack
from tests.schemas import create_builder


@pytest.mark.asyncio
async def test_wait_closed_before_start_is_rejected(
    state_stack: StateStack,
) -> None:
    config = HostConfig(f"direct://unused-{uuid4().hex}")
    host = StoreHost(state_stack.store, config)
    with pytest.raises(StoreStateError, match="not been started"):
        await host.wait_closed()
    await host.stop()


@pytest.mark.asyncio
async def test_failed_registration_can_retry_after_store_boot(
    tmp_path: Path,
) -> None:
    config = (
        create_builder()
        .storage_path(tmp_path)
        .host(HostConfig(f"direct://retry-{uuid4().hex}"))
        .build()
    )
    store = Store(config)
    host = StoreHost(store)
    try:
        with pytest.raises(StoreStateError):
            await host.start()
        assert not host.running
        assert not host.transport.running
        store.boot()
        await host.start()
        assert host.running
        assert host.transport.running
    finally:
        await host.stop()
        store.shutdown()


@pytest.mark.asyncio
async def test_cancelled_closed_waiter_does_not_cancel_other_waiters(
    state_stack: StateStack,
) -> None:
    host = state_stack.host
    entered = asyncio.Event()

    async def wait() -> None:
        entered.set()
        await host.wait_closed()

    cancelled = asyncio.create_task(wait())
    await entered.wait()
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    await host.stop()
    await host.wait_closed()
    assert not host.transport.running


@pytest.mark.asyncio
async def test_concurrent_start_waits_until_transport_is_ready(
    state_stack: StateStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = state_stack.host
    await host.stop()
    original = host.transport.start
    entered = asyncio.Event()
    release = asyncio.Event()
    second_entered = asyncio.Event()
    second_finished = asyncio.Event()

    async def gated_start(owner: StoreHost) -> None:
        entered.set()
        await release.wait()
        await original(owner)

    async def second_start() -> None:
        second_entered.set()
        await host.start()
        second_finished.set()

    monkeypatch.setattr(host.transport, "start", gated_start)
    first = asyncio.create_task(host.start())
    await entered.wait()
    second = asyncio.create_task(second_start())
    try:
        await second_entered.wait()
        assert not second_finished.is_set()
        assert not host.running
    finally:
        release.set()
        await asyncio.gather(first, second)
    assert host.running
    assert host.transport.running


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_first", [False, True])
async def test_concurrent_stop_waits_for_transport_cleanup(
    state_stack: StateStack,
    monkeypatch: pytest.MonkeyPatch,
    cancel_first: bool,
) -> None:
    host = state_stack.host
    original = host.transport.stop
    entered = asyncio.Event()
    release = asyncio.Event()
    second_entered = asyncio.Event()
    stopped = asyncio.Event()

    async def gated_stop() -> None:
        entered.set()
        await release.wait()
        await original()

    async def second_stop() -> None:
        second_entered.set()
        await host.stop()
        stopped.set()

    monkeypatch.setattr(host.transport, "stop", gated_stop)
    first = asyncio.create_task(host.stop())
    await entered.wait()
    second = asyncio.create_task(second_stop())
    closed = asyncio.create_task(host.wait_closed())
    try:
        await second_entered.wait()
        if cancel_first:
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
        assert not stopped.is_set()
        assert not closed.done()
        assert host.transport.running
    finally:
        release.set()
        await asyncio.gather(first, second, return_exceptions=True)
        await closed
    assert not host.transport.running


@pytest.mark.asyncio
async def test_capture_integrity_failure_closes_host_with_original_cause(
    state_stack: StateStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = state_stack.host
    error = RuntimeError("shared capture integrity failure")

    def fail_capture(*args: Any, **kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(host.subscriptions, "capture", fail_capture)
    result = state_stack.store.execute(
        (
            InsertRequestAction(
                "UserSchema",
                "committed-user",
                (("email", "committed@example.com"),),
            ),
        ),
        transaction_id="capture-failure",
    )
    assert result.success
    async with asyncio.timeout(2):
        with pytest.raises(StoreInternalError) as failure:
            await host.wait_closed()
    assert failure.value.__cause__ is error
    assert not host.transport.running
    assert state_stack.store.booted


@pytest.mark.asyncio
async def test_stop_failure_settles_closed_waiters_and_can_retry_cleanup(
    state_stack: StateStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = state_stack.host
    original = host.transport.stop
    error = OSError("listener cleanup failed")

    async def fail_stop() -> None:
        raise error

    monkeypatch.setattr(host.transport, "stop", fail_stop)
    try:
        with pytest.raises(OSError, match="listener cleanup failed"):
            await host.stop()
        async with asyncio.timeout(2):
            with pytest.raises(StoreInternalError) as failure:
                await host.wait_closed()
        assert failure.value.__cause__ is error
    finally:
        monkeypatch.setattr(host.transport, "stop", original)
        await host.stop()
    assert not host.transport.running


@pytest.mark.asyncio
async def test_failure_cleanup_error_preserves_original_failure(
    state_stack: StateStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = state_stack.host
    original = host.transport.stop
    error = RuntimeError("original shared failure")

    async def fail_stop() -> None:
        raise OSError("secondary cleanup failure")

    monkeypatch.setattr(host.transport, "stop", fail_stop)
    host._fail(error)
    try:
        async with asyncio.timeout(2):
            with pytest.raises(StoreInternalError) as failure:
                await host.wait_closed()
        assert failure.value.__cause__ is error
        task = host._failure_task
        if task is not None:
            await task
    finally:
        monkeypatch.setattr(host.transport, "stop", original)
        await host.stop()


@pytest.mark.asyncio
async def test_cancelling_failure_task_does_not_cancel_actual_cleanup(
    state_stack: StateStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = state_stack.host
    original = host.transport.stop
    entered = asyncio.Event()
    release = asyncio.Event()
    error = RuntimeError("shared failure")

    async def gated_stop() -> None:
        entered.set()
        await release.wait()
        await original()

    monkeypatch.setattr(host.transport, "stop", gated_stop)
    host._fail(error)
    try:
        await entered.wait()
        task = host._failure_task
        assert task is not None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        async with asyncio.timeout(2):
            with pytest.raises(StoreInternalError) as failure:
                await host.wait_closed()
    assert failure.value.__cause__ is error
    assert not host.transport.running


@pytest.mark.asyncio
async def test_restart_waits_for_failure_cleanup_without_closing_new_host(
    state_stack: StateStack,
) -> None:
    host = state_stack.host
    host._fail(RuntimeError("failed generation"))
    closing = host._failure_task
    await host.start()
    if closing is not None:
        await closing
    assert host.running
    assert host.transport.running
    endpoint = host.transport.endpoint.get("endpoint", host.endpoint)
    client = StoreClient()
    try:
        await client.connect(endpoint)
        assert client.online
    finally:
        await client.disconnect()
