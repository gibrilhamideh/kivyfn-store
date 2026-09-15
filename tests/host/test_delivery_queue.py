from __future__ import annotations

import asyncio
from collections.abc import Awaitable

import pytest

from kivyfn.store.error import StoreConnectionError
from kivyfn.store.host.client import HostClient, HostClients
from kivyfn.store.protocol.message.event import EventMessage


def message(transaction: str) -> EventMessage:
    return EventMessage("store", (), transaction_id=transaction)


async def started_flush(client: HostClient, started: asyncio.Event) -> None:
    started.set()
    await client.flush()


async def finish(*tasks: Awaitable[object]) -> None:
    async with asyncio.timeout(2):
        await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_barriers_do_not_consume_capacity_or_wait_for_future_messages():
    first_started, first_release = asyncio.Event(), asyncio.Event()
    second_started, second_release = asyncio.Event(), asyncio.Event()
    sent: list[str | None] = []
    failures = []

    async def send(_: str, event: EventMessage) -> None:
        if event.transaction_id == "first":
            first_started.set()
            await first_release.wait()
        else:
            second_started.set()
            await second_release.wait()
        sent.append(event.transaction_id)

    client = HostClient(
        "client",
        send,
        lambda _, event: event,
        lambda *error: failures.append(error),
        max_pending=1,
    )
    flush_started = asyncio.Event()
    first_flush: asyncio.Task[None] | None = None
    try:
        assert client.enqueue(message("first"))
        await finish(first_started.wait())
        first_flush = asyncio.create_task(started_flush(client, flush_started))
        await finish(flush_started.wait())
        assert client.enqueue(message("second"))
        assert not client.closed
        first_release.set()
        await finish(first_flush, second_started.wait())
        assert sent == ["first"]
        second_release.set()
        await finish(client.flush())
        assert sent == ["first", "second"]
        assert failures == []
    finally:
        first_release.set()
        second_release.set()
        await client.close()
        if first_flush is not None:
            await asyncio.gather(first_flush, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_flush_keeps_other_barriers_and_delivery_alive():
    entered, release = asyncio.Event(), asyncio.Event()
    sent = []
    failures = []

    async def send(_: str, event: EventMessage) -> None:
        entered.set()
        await release.wait()
        sent.append(event.transaction_id)

    client = HostClient(
        "client",
        send,
        lambda _, event: event,
        lambda *error: failures.append(error),
        max_pending=1,
    )
    tasks: list[asyncio.Task[None]] = []
    try:
        client.enqueue(message("first"))
        await finish(entered.wait())
        for _ in range(2):
            started = asyncio.Event()
            tasks.append(asyncio.create_task(started_flush(client, started)))
            await finish(started.wait())
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        assert not tasks[1].done()
        release.set()
        await finish(tasks[1])
        assert client.enqueue(message("after cancellation"))
        await finish(client.flush())
        assert sent == ["first", "after cancellation"]
        assert not client.closed and failures == []
    finally:
        release.set()
        await client.close()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_close_settles_pending_flush_and_cancels_inflight_delivery():
    entered, cancelled = asyncio.Event(), asyncio.Event()
    failures = []

    async def send(_: str, event: EventMessage) -> None:
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    client = HostClient(
        "client",
        send,
        lambda _, event: event,
        lambda *error: failures.append(error),
        max_pending=1,
    )
    client.enqueue(message("first"))
    await finish(entered.wait())
    started = asyncio.Event()
    flushing = asyncio.create_task(started_flush(client, started))
    try:
        await finish(started.wait())
        await client.close()
        with pytest.raises(StoreConnectionError, match="closed"):
            await flushing
        assert cancelled.is_set() and client.closed
        assert not client.enqueue(message("closed"))
        with pytest.raises(StoreConnectionError, match="closed"):
            await client.flush()
        await client.close()
        assert failures == []
    finally:
        await client.close()
        await asyncio.gather(flushing, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["prepare", "send"])
async def test_delivery_failure_settles_barriers_and_reports_once(stage: str):
    entered, release = asyncio.Event(), asyncio.Event()
    failure = RuntimeError("injected delivery failure")
    failures = []

    def prepare(_: str, event: EventMessage) -> EventMessage:
        if event.transaction_id == "failing" and stage == "prepare":
            raise failure
        return event

    async def send(_: str, event: EventMessage) -> None:
        if event.transaction_id == "first":
            entered.set()
            await release.wait()
        else:
            raise failure

    client = HostClient(
        "client", send, prepare, lambda *error: failures.append(error)
    )
    flushing: asyncio.Task[None] | None = None
    try:
        client.enqueue(message("first"))
        await finish(entered.wait())
        client.enqueue(message("failing"))
        started = asyncio.Event()
        flushing = asyncio.create_task(started_flush(client, started))
        await finish(started.wait())
        release.set()
        with pytest.raises(StoreConnectionError, match="closed"):
            await flushing
        assert client.closed
        assert failures == [("client", failure)]
        await client.close()
        assert failures == [("client", failure)]
    finally:
        release.set()
        await client.close()
        if flushing is not None:
            await asyncio.gather(flushing, return_exceptions=True)


@pytest.mark.asyncio
async def test_filtered_messages_release_capacity_and_flush_normally():
    prepared = []
    sent = []

    def prepare(_: str, event: EventMessage) -> EventMessage | None:
        prepared.append(event.transaction_id)
        return None if event.transaction_id == "released" else event

    async def send(_: str, event: EventMessage) -> None:
        sent.append(event.transaction_id)

    client = HostClient(
        "client", send, prepare, lambda *_: None, max_pending=1
    )
    try:
        assert client.enqueue(message("released"))
        await finish(client.flush())
        assert client.enqueue(message("active"))
        await finish(client.flush())
        assert prepared == ["released", "active"]
        assert sent == ["active"]
        assert not client.closed
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_slow_client_overflow_does_not_block_healthy_client():
    slow_started = asyncio.Event()
    sent = []
    failures = []

    async def send(client_id: str, event: EventMessage) -> None:
        if client_id == "slow":
            slow_started.set()
            await asyncio.Future()
        sent.append((client_id, event.transaction_id))

    clients = HostClients(
        send,
        lambda _, event: event,
        lambda *error: failures.append(error),
        max_pending=1,
    )
    slow, healthy = clients.add("slow"), clients.add("healthy")
    try:
        clients.enqueue("slow", message("in-flight"))
        await finish(slow_started.wait())
        assert clients.enqueue("slow", message("backlog"))
        assert not clients.enqueue("slow", message("overflow"))
        assert slow.closed
        assert clients.enqueue("healthy", message("continues"))
        await finish(clients.flush("healthy"))
        assert sent == [("healthy", "continues")]
        assert len(failures) == 1 and failures[0][0] == "slow"
        assert not healthy.closed
        assert clients.add("healthy") is healthy
        assert await clients.remove("slow") is slow
        assert await clients.remove("slow") is None
        assert not clients.enqueue("unknown", message("missing"))
        with pytest.raises(StoreConnectionError, match="disconnected"):
            await clients.flush("unknown")
    finally:
        await clients.clear()
    assert len(clients) == 0 and healthy.closed
