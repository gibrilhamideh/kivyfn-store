# kivyfn/store/store.py
from __future__ import annotations

import asyncio
import multiprocessing
from typing import TYPE_CHECKING, Any, TypeVar

from kivyfn.store.client import StoreClient
from kivyfn.store.errors.client import (
    StoreConnectionError,
    StoreStateError,
)
from kivyfn.store.events.subscription import SubscriptionEvents
from kivyfn.store.server import StoreServer
from kivyfn.store.signals.api import SignalEvents
from kivyfn.store.transaction import Transaction


if TYPE_CHECKING:
    from kivyfn.store.address import Address
    from kivyfn.store.builder.config import StoreConfig
    from kivyfn.store.node import Node
    from kivyfn.store.transport.response import Response


T = TypeVar("T")


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

        self._subscription = SubscriptionEvents(self)
        self._signal = SignalEvents(self)

    # -------------------------------------------------
    # Lifecycle
    # -------------------------------------------------

    async def start(
        self,
        root: Node,
        config: StoreConfig,
        log_queue: multiprocessing.Queue,
    ) -> None:
        if self._server is not None:
            raise StoreStateError(
                "Store server already started"
            )

        self._ready_event.clear()

        try:
            self._server = StoreServer(
                root,
                config,
                self.host,
                self.port,
                log_queue,
            )

            await self._server.start()
            await self.connect()

        except Exception as exc:
            await self.stop()

            raise StoreStateError(
                "Store failed to start"
            ) from exc

    async def stop(self) -> None:
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
            raise StoreConnectionError(
                "Store client already connected"
            )

        self._ready_event.clear()

        url = f"http://{self.host}:{self.port}"

        client = StoreClient(
            self,
            url,
        )

        try:
            await client.connect()

        except Exception as exc:
            self._client = None
            self._ready_event.set()

            raise StoreConnectionError(
                "Failed to connect to store"
            ) from exc

        self._client = client
        self._ready_event.set()

    async def ready(self) -> None:
        await self._ready_event.wait()

        if self._client is None:
            raise StoreConnectionError(
                "Client is not connected"
            )

    # -------------------------------------------------
    # Client access
    # -------------------------------------------------

    @property
    def client(self) -> StoreClient:
        if self._client is None:
            raise StoreConnectionError(
                "Store must be connected before use"
            )

        return self._client

    # -------------------------------------------------
    # Read API
    # -------------------------------------------------

    async def snapshot(
        self,
        address: Address[T],
        id: str | None = None,
        key: str | None = None,
        *,
        resolve_foreignkeys: bool = False,
    ) -> Response[T]:
        await self.ready()

        return await self.client.snapshot(
            address=address,
            id=id,
            key=key,
            resolve_foreignkeys=resolve_foreignkeys,
        )

    async def lookup(
        self,
        address: Address[T],
        fields: str | tuple[str, ...],
        value: Any,
        *,
        first: bool = False,
    ) -> Response[T | dict[str, T] | None]:
        await self.ready()

        return await self.client.lookup(
            address=address,
            fields=fields,
            value=value,
            first=first,
        )

    async def to_dict(
        self,
        address: Address[T],
    ) -> Response[T]:
        await self.ready()

        return await self.client.to_dict(
            address,
        )

    # -------------------------------------------------
    # History API
    # -------------------------------------------------

    async def undo(self) -> Response[Any]:
        await self.ready()

        return await self.client.undo()

    async def redo(self) -> Response[Any]:
        await self.ready()

        return await self.client.redo()

    # -------------------------------------------------
    # API facades
    # -------------------------------------------------

    @property
    def transaction(self) -> Transaction:
        return Transaction(
            self,
        )

    @property
    def subscription(self) -> SubscriptionEvents:
        return self._subscription

    @property
    def signal(self) -> SignalEvents:
        return self._signal
