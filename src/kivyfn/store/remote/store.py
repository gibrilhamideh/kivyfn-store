# kivyfn/store/remote/store.py
from __future__ import annotations

from collections.abc import Iterable
from typing import Any, TypeVar

from kivyfn.store.address import Address
from kivyfn.store.error import StoreStateError
from kivyfn.store.query import Query, QueryResult
from kivyfn.store.remote.client import RemoteStoreClient
from kivyfn.store.remote.query import (
    QueryCallback,
    RemoteQuerySubscription,
)
from kivyfn.store.remote.signal import RemoteSignalManager
from kivyfn.store.remote.state import ConnectionState
from kivyfn.store.remote.subscription import RemoteSubscriptionManager
from kivyfn.store.remote.sync import RevisionAudit, SyncSnapshot
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
    """Asynchronous frontend for a remotely hosted store."""

    def __init__(
        self,
        name: str,
        transport: RemoteClientTransport,
        *,
        audit_interval: float = 5.0,
        recovery_delay: float = 1.0,
    ) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError(
                "Remote store name must be a non-empty string"
            )

        self._name = name
        self._client = RemoteStoreClient(
            transport=transport,
            audit_interval=audit_interval,
            recovery_delay=recovery_delay,
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
        await self._client.connect()

    async def disconnect(self) -> None:
        await self._client.disconnect()

    def _require_ready(self) -> None:
        if not self._client.ready:
            raise StoreStateError(
                "Remote store is not synchronized"
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

    @property
    def ready(self) -> bool:
        return self._client.ready

    @property
    def state(self) -> ConnectionState:
        return self._client.state

    # -------------------------------------------------
    # Public managers
    # -------------------------------------------------

    @property
    def subscription(self) -> RemoteSubscriptionManager:
        self._require_ready()
        return self._subscriptions

    @property
    def signal(self) -> RemoteSignalManager:
        self._require_ready()
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
        self._require_ready()
        return await self._client.snapshot(
            address=address,
            id=id,
            key=key,
            resolve_foreignkeys=resolve_foreignkeys,
        )

    async def sync_snapshot(
        self,
        address: Address[T],
        *,
        resolve_foreignkeys: bool = False,
    ) -> Response[SyncSnapshot[Any]]:
        return await self._client.sync_snapshot(
            address=address,
            resolve_foreignkeys=resolve_foreignkeys,
        )

    async def revision_audit(
        self,
        addresses: Iterable[Address[Any]],
    ) -> Response[RevisionAudit]:
        return await self._client.revision_audit(
            addresses,
        )

    async def lookup(
        self,
        address: Address[T],
        fields: str | tuple[str, ...],
        value: Any,
        *,
        first: bool = False,
    ) -> Response[T | dict[str, T] | None]:
        self._require_ready()
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
        self._require_ready()
        return await self._client.to_dict(
            address,
        )

    async def fetch(
        self,
        query: Query,
    ) -> Response[QueryResult]:
        self._require_ready()
        return await self._client.fetch_query(query)

    async def subscribe(
        self,
        query: Query,
        callback: QueryCallback,
    ) -> Response[RemoteQuerySubscription]:
        self._require_ready()
        return await self._client.subscribe_query(
            query,
            callback,
        )

    # -------------------------------------------------
    # Transactions
    # -------------------------------------------------

    async def commit(
        self,
        transaction: TransactionInput,
    ) -> Response[TransactionResult]:
        self._require_ready()
        commands = resolve_commands(
            transaction,
        )
        return await self._client.transaction(
            commands,
        )

    # -------------------------------------------------
    # Undo / redo
    # -------------------------------------------------

    async def undo(self) -> Response[Any]:
        self._require_ready()
        return await self._client.undo()

    async def redo(self) -> Response[Any]:
        self._require_ready()
        return await self._client.redo()

    # -------------------------------------------------
    # Internal access
    # -------------------------------------------------

    @property
    def client(self) -> RemoteStoreClient:
        return self._client

    # -------------------------------------------------
    # Helpers
    # -------------------------------------------------

    def transaction(self) -> Transaction:
        return Transaction()
