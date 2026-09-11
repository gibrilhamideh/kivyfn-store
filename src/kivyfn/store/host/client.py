# src/kivyfn/store/host/client.py
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from threading import Lock
from typing import TypeAlias

from kivyfn.logging import get_logger
from kivyfn.store.protocol.message.event import EventMessage


HostEventSender: TypeAlias = Callable[[str, EventMessage], Awaitable[None]]
HostEventFilter: TypeAlias = Callable[[str, EventMessage], EventMessage | None]
HostClientFailureHandler: TypeAlias = Callable[[str, Exception], None]

logger = get_logger(__name__)


class HostClient:
    """One connected client's ordered event delivery.

    Create and close the client on the host event loop. enqueue() may be
    called from a Store commit thread; calls enter the queue in call order.

    prepare() filters subscriptions immediately before delivery. It owns
    any Store locking needed for that filtering. send() owns transport I/O.
    on_error() reports delivery failure synchronously so the owner can
    disconnect the client or stop the host and make loss of sync observable.
    """

    def __init__(
        self,
        client_id: str,
        send: HostEventSender,
        prepare: HostEventFilter,
        on_error: HostClientFailureHandler,
    ) -> None:
        self._client_id = client_id
        self._send = send
        self._prepare = prepare
        self._on_error = on_error
        self._loop = asyncio.get_running_loop()
        self._lock = Lock()
        self._closed = False
        self._queue: asyncio.Queue[EventMessage] = asyncio.Queue()
        self._task = self._loop.create_task(self._deliver())

    @property
    def client_id(self) -> str:
        return self._client_id

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def enqueue(self, message: EventMessage) -> bool:
        """Schedule a captured message, returning False after closure."""
        with self._lock:
            if self._closed:
                return False

            self._loop.call_soon_threadsafe(self._enqueue, message)
            return True

    def stop(self) -> None:
        """Stop accepting events and cancel delivery on the host loop."""
        with self._lock:
            self._closed = True

        self._discard()

        if self._task is not asyncio.current_task():
            self._task.cancel()

    async def close(self) -> None:
        """Stop delivery and wait for its task, including during disconnect."""
        self.stop()

        if self._task is not asyncio.current_task():
            await asyncio.gather(self._task, return_exceptions=True)

    def _enqueue(self, message: EventMessage) -> None:
        if not self.closed:
            self._queue.put_nowait(message)

    def _discard(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return

            self._queue.task_done()

    async def _deliver(self) -> None:
        try:
            while not self.closed:
                message = await self._queue.get()

                try:
                    prepared = self._prepare(self.client_id, message)

                    if prepared is not None:
                        await self._send(self.client_id, prepared)
                finally:
                    self._queue.task_done()

        except Exception as error:
            self.stop()

            try:
                self._on_error(self.client_id, error)
            except Exception:
                logger.exception(
                    "Store client %s failure handler failed", self.client_id
                )
        finally:
            self.stop()


class HostClients:
    """Registry of connected sessions and their delivery tasks.

    add(), remove(), and clear() run on the host loop. Lookup and enqueue
    may also run on Store commit threads. Stop accepting new connections
    before clear(); a transport disconnect releases its subscriptions too.
    """

    def __init__(
        self,
        send: HostEventSender,
        prepare: HostEventFilter,
        on_error: HostClientFailureHandler,
    ) -> None:
        self._send = send
        self._prepare = prepare
        self._on_error = on_error
        self._lock = Lock()
        self._clients: dict[str, HostClient] = {}

    def add(self, client_id: str) -> HostClient:
        """Register a connected client, reusing its existing session."""
        with self._lock:
            client = self._clients.get(client_id)

            if client is None:
                client = HostClient(
                    client_id, self._send, self._prepare, self._on_error
                )
                self._clients[client_id] = client

            return client

    def get(self, client_id: str) -> HostClient | None:
        with self._lock:
            return self._clients.get(client_id)

    def enqueue(self, client_id: str, message: EventMessage) -> bool:
        """Queue an event when its client is still registered and open."""
        client = self.get(client_id)
        return client.enqueue(message) if client is not None else False

    async def remove(self, client_id: str) -> HostClient | None:
        """Detach a disconnected session and discard its pending delivery."""
        with self._lock:
            client = self._clients.pop(client_id, None)

        if client is not None:
            await client.close()

        return client

    async def clear(self) -> None:
        """Close every session, including when called from an event handler."""
        with self._lock:
            clients = tuple(self._clients.values())
            self._clients.clear()

        for client in clients:
            client.stop()

        for client in clients:
            await client.close()

    def __len__(self) -> int:
        with self._lock:
            return len(self._clients)
