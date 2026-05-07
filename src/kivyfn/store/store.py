from __future__ import annotations
from typing import TYPE_CHECKING, Any

import asyncio

from kivyfn.store.server import StoreServer
from kivyfn.store.client import StoreClient
from kivyfn.store.transaction import Transaction
from kivyfn.store.events.subscription import SubscriptionEvents


if TYPE_CHECKING:
    from kivyfn.store.address import Address
    from kivyfn.store.node import Node
    from kivyfn.store.builder.config import StoreConfig
    from kivyfn.store.transport.response import Response


class Store:
    def __init__(self, host="127.0.0.1", port=8765) -> None:
        self.host = host
        self.port = port
        self._client: StoreClient | None = None
        self._server: StoreServer | None = None

        self._ready_event = asyncio.Event()
        self._startup_error: Exception | None = None
        self._starting = False
        self._stopping = False

    # -------------------------------------------------
    # Lifecycle
    # -------------------------------------------------

    async def start(self, root: Node, config: StoreConfig) -> None:
        if self._server is not None:
            raise RuntimeError("Store server already started")

        self._ready_event.clear()
        self._startup_error = None
        self._starting = True

        try:
            self._server = StoreServer(root, config, self.host, self.port)

            await self._server.start()
            await self.connect()

            self._ready_event.set()

        except Exception as exc:
            self._startup_error = exc
            self._ready_event.set()
            raise

        finally:
            self._starting = False

    async def stop(self) -> None:
        """
        Stops the store server and disconnects the client.

        Safe to call once.
        """

        self._stopping = True

        # Disconnect client first
        if self._client is not None:
            try:
                await self._client.disconnect()
            finally:
                self._client = None

        # Then stop server
        if self._server is not None:
            try:
                self._server.stop()
            finally:
                self._server = None

    async def connect(self) -> None:
        if self._client is not None:
            raise RuntimeError("Store client already connected")

        host = f"http://{self.host}:{self.port}"
        self._client = StoreClient(self, host)
        await self._client.connect()

    async def ready(self) -> None:
        if self._startup_error is not None:
            raise RuntimeError("Store failed to start") from self._startup_error

        if self._stopping:
            raise RuntimeError("Store is stopping")

        if self._server is None:
            raise RuntimeError("Store has not been started")

        await self._ready_event.wait()

        if self._client is None:
            raise RuntimeError("Store is not connected")

    # -------------------------------------------------
    # Client Access
    # -------------------------------------------------
    @property
    def client(self) -> StoreClient:
        if self._client is None:
            raise RuntimeError("Store must be connected before use")
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
        id=None,
        key=None,
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

    async def to_dict(self, address: Address) -> Response[dict]:
        await self.ready()
        return await self.client.to_dict(address)

    @property
    def transaction(self) -> Transaction:
        return Transaction(self, self.client)

    @property
    def subscription(self) -> SubscriptionEvents:
        return SubscriptionEvents(self, self.client)
