# src/kivyfn/store/host/client.py
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeAlias

from kivyfn.logging import get_logger

from kivyfn.store.error import StoreConnectionError
from kivyfn.store.protocol.message.event import EventMessage

HostEventSender: TypeAlias = Callable[[str, EventMessage], Awaitable[None]]
HostEventFilter: TypeAlias = Callable[[str, EventMessage], EventMessage | None]
HostClientFailureHandler: TypeAlias = Callable[[str, Exception], None]
Delivery: TypeAlias = EventMessage | asyncio.Future[None]
logger = get_logger(__name__)


class HostClient:
    """Bounded ordered delivery with a barrier for request completion."""

    def __init__(
        self,
        client_id: str,
        send: HostEventSender,
        prepare: HostEventFilter,
        on_error: HostClientFailureHandler,
        max_pending: int = 1000,
    ) -> None:
        self._client_id = client_id
        self._send = send
        self._prepare = prepare
        self._on_error = on_error
        self._closed = False
        self._queue: asyncio.Queue[Delivery] = asyncio.Queue()
        self._max_pending = max_pending
        self._pending_messages = 0
        self._barriers: set[asyncio.Future[None]] = set()
        self._task = asyncio.create_task(self._deliver())

    @property
    def client_id(self) -> str:
        return self._client_id

    @property
    def closed(self) -> bool:
        return self._closed

    def enqueue(self, message: EventMessage) -> bool:
        if self.closed:
            return False
        if self._pending_messages >= self._max_pending:
            self._fail(
                StoreConnectionError("Store client delivery queue full")
            )
            return False
        self._pending_messages += 1
        self._queue.put_nowait(message)
        return True

    async def flush(self) -> None:
        """Wait until events already queued for this client have been sent."""
        if self.closed:
            raise StoreConnectionError("Store client delivery is closed")
        barrier = asyncio.get_running_loop().create_future()
        self._barriers.add(barrier)
        # Completion markers do not consume the message backlog allowance.
        self._queue.put_nowait(barrier)
        try:
            await asyncio.shield(barrier)
        finally:
            self._barriers.discard(barrier)
            if not barrier.done():
                barrier.cancel()

    def stop(self) -> None:
        if self.closed:
            return
        self._closed = True
        error = StoreConnectionError("Store client delivery is closed")
        for future in tuple(self._barriers):
            if not future.done():
                future.set_exception(error)
        while not self._queue.empty():
            self._queue.get_nowait()
        self._pending_messages = 0
        if self._task is not asyncio.current_task():
            self._task.cancel()

    async def close(self) -> None:
        self.stop()
        if self._task is not asyncio.current_task():
            await asyncio.gather(self._task, return_exceptions=True)

    def _fail(self, error: Exception) -> None:
        self.stop()
        try:
            self._on_error(self.client_id, error)
        except Exception:
            logger.exception("Store client failure handler failed")

    async def _deliver(self) -> None:
        try:
            while not self.closed:
                item = await self._queue.get()
                if isinstance(item, asyncio.Future):
                    if not item.done():
                        item.set_result(None)
                    continue
                self._pending_messages -= 1
                prepared = self._prepare(self.client_id, item)
                if prepared is not None:
                    await self._send(self.client_id, prepared)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._fail(error)
        finally:
            self.stop()


class HostClients:
    """Connected clients and their independent ordered delivery queues."""

    def __init__(
        self,
        send: HostEventSender,
        prepare: HostEventFilter,
        on_error: HostClientFailureHandler,
        max_pending: int = 1000,
    ) -> None:
        if max_pending < 1:
            raise ValueError("max_pending must be positive")
        self._send = send
        self._prepare = prepare
        self._on_error = on_error
        self._max_pending = max_pending
        self._clients: dict[str, HostClient] = {}

    def add(self, client_id: str) -> HostClient:
        client = self._clients.get(client_id)
        if client is None or client.closed:
            client = HostClient(
                client_id,
                self._send,
                self._prepare,
                self._on_error,
                self._max_pending,
            )
            self._clients[client_id] = client
        return client

    def get(self, client_id: str) -> HostClient | None:
        return self._clients.get(client_id)

    def enqueue(self, client_id: str, message: EventMessage) -> bool:
        client = self._clients.get(client_id)
        return client.enqueue(message) if client is not None else False

    async def flush(self, client_id: str) -> None:
        client = self._clients.get(client_id)
        if client is None:
            raise StoreConnectionError("Store client is disconnected")
        await client.flush()

    async def remove(self, client_id: str) -> HostClient | None:
        client = self._clients.pop(client_id, None)
        if client is not None:
            await client.close()
        return client

    async def clear(self) -> None:
        clients = tuple(self._clients.values())
        self._clients.clear()
        for client in clients:
            client.stop()
        for client in clients:
            await client.close()

    def __len__(self) -> int:
        return len(self._clients)
