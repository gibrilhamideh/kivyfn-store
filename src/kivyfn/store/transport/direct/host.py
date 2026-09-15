# src/kivyfn/store/transport/direct/host.py
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, TypeAlias
from uuid import uuid4

from kivyfn.logging import get_logger

from kivyfn.store.error import StoreConnectionError, StoreStateError
from kivyfn.store.protocol.message.event import EventMessage
from kivyfn.store.protocol.message.requests import RequestBatch
from kivyfn.store.protocol.message.responses import ResponseBatch
from kivyfn.store.transport.direct.registry import (
    register_host,
    unregister_host,
)
from kivyfn.store.transport.host import HostTransport

if TYPE_CHECKING:
    from kivyfn.store.host.host import StoreHost


logger = get_logger(__name__)


DirectMessageHandler: TypeAlias = Callable[[EventMessage], Awaitable[None]]

DirectCloseHandler: TypeAlias = Callable[[], Awaitable[None]]

DirectClientHandlers: TypeAlias = tuple[
    DirectMessageHandler, DirectCloseHandler
]


class DirectHostTransport(HostTransport):
    """Connect in-process clients directly to the injected StoreHost."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._running = False
        self._clients: dict[str, DirectClientHandlers] = {}

    @property
    def running(self) -> bool:
        return self._running

    @property
    def endpoint(self) -> dict[str, Any]:
        return {"transport": "direct", "name": self.name}

    async def start(self, store_host: StoreHost) -> None:
        if self.running:
            if self.store_host is not store_host:
                raise StoreStateError("Transport has another running host")

            return

        self.store_host = store_host

        register_host(self.name, self)

        self._running = True

    async def stop(self) -> None:
        if not self.running:
            return

        unregister_host(self.name, self)

        self._running = False

        clients = tuple(self._clients.items())

        self._clients.clear()

        for client_id, (_, close_handler) in clients:
            try:
                await self.store_host.disconnect(client_id)

            except Exception:
                logger.exception("Store client cleanup failed: %s", client_id)

            try:
                await close_handler()

            except Exception:
                logger.exception("Direct client close handler failed")

    async def emit(self, client_id: str, message: EventMessage) -> None:
        handlers = self._clients.get(client_id)

        if not self.running or handlers is None:
            raise StoreConnectionError("Store client is disconnected")

        message_handler, _ = handlers

        await message_handler(message)

    async def connect_client(
        self,
        message_handler: DirectMessageHandler,
        close_handler: DirectCloseHandler,
        *,
        protocol_version: int | None = None,
    ) -> str:
        self._require_running()

        client_id = uuid4().hex

        self._clients[client_id] = (message_handler, close_handler)

        try:
            await self.store_host.connect(
                client_id,
                send=self.emit,
                close=self.disconnect,
                protocol_version=protocol_version,
            )

        except BaseException:
            self._clients.pop(client_id, None)

            await self.store_host.disconnect(client_id)

            raise

        return client_id

    async def disconnect(self, client_id: str) -> None:
        handlers = self._clients.pop(client_id, None)
        if handlers is None:
            return
        try:
            await self.store_host.disconnect(client_id)
        finally:
            await handlers[1]()

    async def disconnect_client(self, client_id: str) -> None:
        if self._clients.pop(client_id, None) is not None:
            await self.store_host.disconnect(client_id)

    async def request(
        self, client_id: str, request: RequestBatch
    ) -> ResponseBatch:
        self._require_running()

        if client_id not in self._clients:
            raise StoreConnectionError("Store client is disconnected")

        return await self.store_host.request(client_id, request)

    def _require_running(self) -> None:
        if not self.running:
            raise StoreStateError(f"Direct endpoint is stopped: {self.name}")
