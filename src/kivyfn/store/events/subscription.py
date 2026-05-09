from __future__ import annotations
from typing import TYPE_CHECKING, Callable, Any

import weakref
import asyncio

from kivyfn.store.constants import InstructionOpCode
from kivyfn.store.utils.tools.uuid import generate_uuid

if TYPE_CHECKING:
    from kivyfn.store import Store
    from kivyfn.store.node import Node
    from kivyfn.store.events.event import StoreEvent
    from kivyfn.store.events.listener import Listener
    from kivyfn.store.client import StoreClient
    from kivyfn.store.address import Address


class Subscription:
    __slots__ = ("_store", "_client", "_token", "_event_token", "_callback_ref")

    def __init__(
        self,
        store: Store,
        client: StoreClient,
        token: str,
        event_token: str,
        callback: Callable[[Any], None],
    ) -> None:
        self._store = store
        self._client = client
        self._token = token
        self._event_token = event_token
        self._callback_ref = weakref.WeakMethod(callback)

    async def dispose(self) -> None:
        await self._store.ready()
        await self._client.unsubscribe(self._token)

    def dispatch(self, event: StoreEvent) -> None:
        callback = self._callback_ref()

        if callback is None:
            asyncio.create_task(self.dispose())
            return

        callback(event)

    @property
    def token(self) -> str:
        return self._token

    @property
    def event_token(self) -> str:
        return self._event_token

    def __repr__(self) -> str:
        return f"<Subscription token={self._token!r} event_token={self._event_token!r}>"


class SubscriptionEvents:
    def __init__(self, store: Store) -> None:
        self._store = store

    async def set(self, callback, address: Address, key: str | None = None):
        await self._store.ready()
        return await self._store.client.subscribe(
            opcode=InstructionOpCode.SET,
            callback=callback,
            address=address,
            key=key,
        )

    async def setrow(
        self,
        callback,
        address: Address,
        id: str | None = None,
        key: str | None = None,
        depth: int | None = 0,
        propagate_foreignkeys: bool = False
    ):
        await self._store.ready()
        return await self._store.client.subscribe(
            opcode=InstructionOpCode.SETROW,
            callback=callback,
            address=address,
            id=id,
            key=key,
            depth=depth,
            propagate_foreignkeys=propagate_foreignkeys
        )

    async def insert(self, callback, address: Address, resolve_foreignkeys=False):
        await self._store.ready()
        return await self._store.client.subscribe(
            opcode=InstructionOpCode.INSERT,
            callback=callback,
            address=address,
            resolve_foreignkeys=resolve_foreignkeys,
        )

    async def remove(self, callback, address: Address):
        await self._store.ready()
        return await self._store.client.subscribe(
            opcode=InstructionOpCode.REMOVE, callback=callback, address=address
        )


class EngineSubscription:
    """
    Server-side subscription handle.

    Owned by SubscriptionManager.

    It links:
    - a node
    - a listener
    - a unique subscription token

    It does not unsubscribe itself from the node because nodes no longer own
    listeners. Cleanup is handled by SubscriptionManager.
    """

    __slots__ = ("_node", "_token", "_listener")

    def __init__(self, node: Node, listener: Listener) -> None:
        self._node = node
        self._listener = listener
        self._token = generate_uuid()

    def dispose(self) -> None:
        pass

    @property
    def node(self) -> Node:
        return self._node

    @property
    def listener(self) -> Listener:
        return self._listener

    @property
    def token(self) -> str:
        return self._token

    @property
    def room(self) -> str:
        return self._listener.room
