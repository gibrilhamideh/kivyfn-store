# src/kivyfn/store/host/client.py
from __future__ import annotations

import asyncio

from collections.abc import Awaitable, Callable
from typing import TypeAlias

from kivyfn.logging import get_logger
from kivyfn.store.protocol.message.event import EventMessage


HostEventSender: TypeAlias = Callable[[str, EventMessage], Awaitable[None]]
HostEventFilter: TypeAlias = Callable[[str, EventMessage], EventMessage | None]
HostClientFailureHandler: TypeAlias = Callable[[str, Exception], None]


logger = get_logger(__name__)


class HostClient:
    """One connected client's ordered event delivery."""

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

        self._closed = False
        self._queue: asyncio.Queue[EventMessage] = asyncio.Queue()
        self._task = asyncio.create_task(self._deliver())

    @property
    def client_id(self) -> str:
        return self._client_id

    @property
    def closed(self) -> bool:
        return self._closed

    def enqueue(self, message: EventMessage) -> bool:
        if self._closed:
            return False

        self._queue.put_nowait(message)
        return True

    def stop(self) -> None:
        if self._closed:
            return

        self._closed = True
        self._queue = asyncio.Queue()

        if self._task is not asyncio.current_task():
            self._task.cancel()

    async def close(self) -> None:
        self.stop()

        if self._task is not asyncio.current_task():
            await asyncio.gather(self._task, return_exceptions=True)

    async def _deliver(self) -> None:
        try:
            while not self._closed:
                message = await self._queue.get()
                prepared = self._prepare(self._client_id, message)

                if prepared is not None:
                    await self._send(self._client_id, prepared)

        except asyncio.CancelledError:
            raise

        except Exception as error:
            self._closed = True

            try:
                self._on_error(self._client_id, error)
            except Exception:
                logger.exception(
                    "Store client %s failure handler failed", self._client_id
                )

        finally:
            self._closed = True


class HostClients:
    """Connected Host clients and their ordered event queues."""

    def __init__(
        self,
        send: HostEventSender,
        prepare: HostEventFilter,
        on_error: HostClientFailureHandler,
    ) -> None:
        self._send = send
        self._prepare = prepare
        self._on_error = on_error
        self._clients: dict[str, HostClient] = {}

    def add(self, client_id: str) -> HostClient:
        client = self._clients.get(client_id)

        if client is None or client.closed:
            client = HostClient(
                client_id, self._send, self._prepare, self._on_error
            )
            self._clients[client_id] = client

        return client

    def get(self, client_id: str) -> HostClient | None:
        return self._clients.get(client_id)

    def enqueue(self, client_id: str, message: EventMessage) -> bool:
        client = self._clients.get(client_id)

        if client is None:
            return False

        return client.enqueue(message)

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
