from __future__ import annotations
from typing import TYPE_CHECKING, Any

from kivyfn.store.client import StoreClient
from kivyfn.store.events.subscription import SubscriptionEvents
from kivyfn.store.server import StoreServer
from kivyfn.store.transaction import Transaction
from kivyfn.store.errors.client import (
    StoreStateError,
    StoreConnectionError,
)

import asyncio
import multiprocessing

if TYPE_CHECKING:
    from kivyfn.store.address import Address
    from kivyfn.store.builder.config import StoreConfig
    from kivyfn.store.node import Node
    from kivyfn.store.transport.response import Response


class Store:
    def __init__(
            self,
            host: str = "127.0.0.1",
            port: int = 8765,
    ) -> None:

        self.host = host
        self.port = port

        self._client: StoreClient | None = None
        self._server: StoreServer | None = None

        self._ready_event = asyncio.Event()

    # -------------------------------------------------
    # Lifecycle
    # -------------------------------------------------
    async def start(self, root: Node, config: StoreConfig, log_queue: multiprocessing.Queue) -> None:
        if self._server is not None:
            raise StoreStateError("Store server already started")

        self._ready_event.clear()

        try:
            self._server = StoreServer(root, config, self.host, self.port, log_queue)

            await self._server.start()
            await self.connect()

        except Exception as exc:
            await self.stop()
            self._ready_event.set()
            raise StoreStateError("Store failed to start") from exc


    async def stop(self) -> None:
        """
        Stops the store client and server.
        Safe to call multiple times.
        """


        if self._client is not None:
            try:
                await self._client.disconnect()
            finally:
                self._client = None

        if self._server is not None:
            try:
                self._server.stop()
            finally:
                self._server = None

        self._ready_event.clear()

    async def connect(self) -> None:
        if self._client is not None:
            raise StoreConnectionError("Store client already connected")

        self._ready_event.clear()

        host = f"http://{self.host}:{self.port}"
        client = StoreClient(self, host)

        try:
            await client.connect()
        except Exception as exc:
            self._client = None
            self._ready_event.set()
            raise StoreConnectionError("Failed to connect to store") from exc

        self._client = client
        self._ready_event.set()

    async def ready(self) -> None:

        await self._ready_event.wait()
        if self._client is None:
            raise StoreConnectionError("Client is not connected")


    # -------------------------------------------------
    # Client Access
    # -------------------------------------------------

    @property
    def client(self) -> StoreClient:
        if self._client is None:
            raise StoreConnectionError("Store must be connected before use")

        return self._client

    # -------------------------------------------------
    # API Delegation
    # -------------------------------------------------

    async def undo(self) -> Response[Any]:
        await self.ready()
        return await self.client.undo()

    async def redo(self) -> Response[Any]:
        await self.ready()
        return await self.client.redo()

    async def snapshot(
        self,
        address: Address,
        id: str | None = None,
        key: str | None = None,
        *,
        resolve_foreignkeys: bool = False,
    ) -> Response[Any]:
        await self.ready()

        return await self.client.snapshot(
            address,
            id=id,
            key=key,
            resolve_foreignkeys=resolve_foreignkeys,
        )

    async def to_dict(self, address: Address) -> Response[dict[str, Any]]:
        await self.ready()
        return await self.client.to_dict(address)

    @property
    def transaction(self) -> Transaction:
        return Transaction(self)

    @property
    def subscription(self) -> SubscriptionEvents:
        return SubscriptionEvents(self)
