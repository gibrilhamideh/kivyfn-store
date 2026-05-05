from __future__ import annotations
from typing import TYPE_CHECKING, Callable, Any

import weakref
import asyncio

from kivyfn.store.constants import InstructionOpCode
from kivyfn.store.utils.tools.uuid import generate_uuid

if TYPE_CHECKING:
    from kivyfn.store.node import Node
    from kivyfn.store.events import NodeEvent
    from kivyfn.store.events.listener import Listener
    from kivyfn.store.store.client import StoreClient
    from kivyfn.store.address.address import Address


class Subscription:
    __slots__ = ("_client", "_token", "_callback_ref")

    def __init__(
        self,
        client: StoreClient,
        token: str,
        callback: Callable[[Any], None],
    ) -> None:
        self._client = client
        self._token = token
        self._callback_ref = weakref.WeakMethod(callback)

    async def dispose(self) -> None:
        await self._client.unsubscribe(self._token)

    def dispatch(self, event: NodeEvent) -> None:
        callback = self._callback_ref()
        if callback is None:
            asyncio.create_task(self.dispose())
            return

        callback(event)

    @property
    def token(self) -> str:
        return self._token

    def __repr__(self) -> str:
        return f"<Subscription token={self._token!r} active={self._callback_ref}>"


class SubscriptionEvents:
    def __init__(self, client: StoreClient) -> None:
        self._client = client

    async def set(self, callback, address: Address, key: str | None = None):
        return await self._client.subscribe(
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
    ):
        return await self._client.subscribe(
            opcode=InstructionOpCode.SETROW,
            callback=callback,
            address=address,
            id=id,
            key=key,
            depth=depth,
        )

    async def insert(self, callback, address: Address, resolve_foreignkeys=False):
        return await self._client.subscribe(
            opcode=InstructionOpCode.INSERT,
            callback=callback,
            address=address,
            resolve_foreignkeys=resolve_foreignkeys,
        )

    async def remove(self, callback, address: Address):
        return await self._client.subscribe(
            opcode=InstructionOpCode.REMOVE, callback=callback, address=address
        )


class EngineSubscription:
    """
    Opaque subscription handle.

    Returned by Node.subscribe().

    The only public API:
        - dispose()

    Developers must not interact with tokens directly.
    """

    __slots__ = ("_node", "_token", "_listener")

    def __init__(self, node: Node, listener: Listener) -> None:
        self._node = node
        self._listener = listener
        self._token = generate_uuid()

    def dispose(self):
        self._node.unsubscribe(self._listener)

    @property
    def listener(self):
        return self._listener

    @property
    def token(self):
        return self._token

    @property
    def room(self):
        return self._listener.room
