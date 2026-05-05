from __future__ import annotations
import asyncio
from os import wait
from typing import TYPE_CHECKING, Any

from kivyfn.store.store.server import StoreServer
from kivyfn.store.store.client import StoreClient
from kivyfn.store.core.transaction import Transaction
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

    # -------------------------------------------------
    # Lifecycle
    # -------------------------------------------------

    def start(
        self,
        root: Node,
        config: StoreConfig,
    ) -> None:
        if self._server is not None:
            raise RuntimeError("Store server already started")

        self._server = StoreServer(root, config, self.host, self.port)
        self._server.start()

    async def stop(self) -> None:
        """
        Stops the store server and disconnects the client.

        Safe to call once.
        """
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
        self._client = StoreClient(host)
        await self._client.connect()

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
        return await self.client.undo()

    async def redo(self) -> Response[Any]:
        return await self.client.redo()

    async def snapshot(
        self,
        address: Address,
        id=None,
        key=None,
        *,
        resolve_foreignkeys: bool = False,
    ) -> Response[Any]:
        return await self.client.snapshot(
            address,
            id=id,
            key=key,
            resolve_foreignkeys=resolve_foreignkeys,
        )

    async def to_dict(self, address: Address) -> Response[dict]:
        return await self.client.to_dict(address)

    def transaction(self) -> Transaction:
        return Transaction(self.client)

    @property
    def subscription(self) -> SubscriptionEvents:
        return SubscriptionEvents(self.client)
