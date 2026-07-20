# kivyfn/store/remote/store.py
from __future__ import annotations

from typing import Any, TypeVar

from kivyfn.store.address import Address
from kivyfn.store.error import StoreStateError
from kivyfn.store.remote.client import RemoteStoreClient
from kivyfn.store.remote.signal import RemoteSignalManager
from kivyfn.store.remote.subscription import RemoteSubscriptionManager
from kivyfn.store.remote.transport import RemoteClientTransport
from kivyfn.store.response import Response
from kivyfn.store.transaction import (
    Transaction,
    TransactionInput,
    TransactionResult,
    resolve_commands,
)


T = TypeVar("T")


class RemoteStore:
    """
    Transport-independent asynchronous frontend for a remotely hosted store.

    RemoteStore owns:

    - one remote client connection;
    - remote subscriptions;
    - remote signal bindings.

    It has no knowledge of where or how the server is hosted. The server may
    run in another process, another application, another machine, or a cloud
    environment.

    Expected store-operation failures are returned as FailureResponse values
    by RemoteStoreClient. Lifecycle, transport, timeout, connection, and
    protocol failures remain exceptions.
    """

    def __init__(
        self,
        name: str,
        transport: RemoteClientTransport,
    ) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError(
                "Remote store name must be a non-empty string"
            )

        self._name = name

        self._client = RemoteStoreClient(
            transport=transport,
        )

        self._subscriptions = RemoteSubscriptionManager(
            self._client,
        )

        self._signals = RemoteSignalManager(
            self._client,
        )

    # -------------------------------------------------
    # Lifecycle
    # -------------------------------------------------

    async def connect(self) -> None:
        """
        Connect to the remote store server.

        Calling connect while already connected is safe.
        """
        await self._client.connect()

    async def disconnect(self) -> None:
        """
        Disconnect from the remote store server.

        Calling disconnect while already disconnected is safe.
        """
        await self._client.disconnect()

    def _require_connected(self) -> None:
        if not self._client.connected:
            raise StoreStateError(
                "Remote store is not connected"
            )

    # -------------------------------------------------
    # State
    # -------------------------------------------------

    @property
    def name(self) -> str:
        return self._name

    @property
    def connected(self) -> bool:
        return self._client.connected

    # -------------------------------------------------
    # Public managers
    # -------------------------------------------------

    @property
    def subscription(
        self,
    ) -> RemoteSubscriptionManager:
        self._require_connected()
        return self._subscriptions

    @property
    def signal(
        self,
    ) -> RemoteSignalManager:
        self._require_connected()
        return self._signals

    # -------------------------------------------------
    # Reads
    # -------------------------------------------------

    async def snapshot(
        self,
        address: Address[T],
        id: str | None = None,
        key: str | None = None,
        *,
        resolve_foreignkeys: bool = False,
    ) -> Response[Any]:
        self._require_connected()

        return await self._client.snapshot(
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
        self._require_connected()

        return await self._client.lookup(
            address=address,
            fields=fields,
            value=value,
            first=first,
        )

    async def to_dict(
        self,
        address: Address[T],
    ) -> Response[T]:
        self._require_connected()

        return await self._client.to_dict(
            address,
        )

    # -------------------------------------------------
    # Transactions
    # -------------------------------------------------

    async def commit(
        self,
        transaction: TransactionInput,
    ) -> Response[TransactionResult]:
        self._require_connected()

        commands = resolve_commands(
            transaction,
        )

        return await self._client.transaction(
            commands,
        )

    # -------------------------------------------------
    # Undo / Redo
    # -------------------------------------------------

    async def undo(
        self,
    ) -> Response[Any]:
        self._require_connected()

        return await self._client.undo()

    async def redo(
        self,
    ) -> Response[Any]:
        self._require_connected()

        return await self._client.redo()

    # -------------------------------------------------
    # Internal access
    # -------------------------------------------------

    @property
    def client(
        self,
    ) -> RemoteStoreClient:
        return self._client

    # -------------------------------------------------
    # Helpers
    # -------------------------------------------------

    def transaction(
        self,
    ) -> Transaction:
        return Transaction()
