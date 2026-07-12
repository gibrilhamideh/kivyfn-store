# kivyfn/store/signals/binding.py
from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Awaitable,
    Callable,
    Generic,
    TypeAlias,
    TypeVar,
)

from kivyfn.store.signals.event import SignalEvent
from kivyfn.store.utils.tools.uuid import generate_uuid


if TYPE_CHECKING:
    from kivyfn.store import Store
    from kivyfn.store.client import StoreClient
    from kivyfn.store.node import Node
    from kivyfn.store.signals.listener import SignalListener


T = TypeVar("T")


SignalCallback: TypeAlias = Callable[
    [SignalEvent[T]],
    None | Awaitable[None],
]


class SignalBinding(Generic[T]):
    __slots__ = (
        "_store",
        "_client",
        "_token",
        "_event_token",
        "_callback",
        "_disposed",
    )

    def __init__(
        self,
        store: Store,
        client: StoreClient,
        token: str,
        event_token: str,
        callback: SignalCallback[T],
    ) -> None:
        self._store = store
        self._client = client
        self._token = token
        self._event_token = event_token
        self._callback = callback
        self._disposed = False

    async def dispose(self) -> None:
        if self._disposed:
            return

        await self._store.ready()

        result = await self._client.unbind_signal(
            self._token,
        )

        if result.ok:
            self._disposed = True

    async def dispatch(
        self,
        event: SignalEvent[T],
    ) -> None:
        if self._disposed:
            return

        result = self._callback(
            event,
        )

        if result is not None:
            await result

    @property
    def token(self) -> str:
        return self._token

    @property
    def event_token(self) -> str:
        return self._event_token

    @property
    def disposed(self) -> bool:
        return self._disposed


class EngineSignalBinding:
    __slots__ = (
        "_node",
        "_listener",
        "_token",
    )

    def __init__(
        self,
        node: Node,
        listener: SignalListener,
    ) -> None:
        self._node = node
        self._listener = listener
        self._token = generate_uuid()

    @property
    def node(self) -> Node:
        return self._node

    @property
    def listener(self) -> SignalListener:
        return self._listener

    @property
    def token(self) -> str:
        return self._token

    @property
    def room(self) -> str:
        return self._listener.room
