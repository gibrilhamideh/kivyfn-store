# kivyfn/store/signals/api.py
from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    TypeAlias,
    TypeVar,
)

from kivyfn.store.signals.binding import SignalBinding
from kivyfn.store.signals.event import SignalEvent


if TYPE_CHECKING:
    from kivyfn.store import Store
    from kivyfn.store.address import Address
    from kivyfn.store.transport.response import Response


T = TypeVar("T")


SignalCallback: TypeAlias = Callable[
    [SignalEvent[T]],
    None | Awaitable[None],
]


class SignalEvents:
    def __init__(
        self,
        store: Store,
    ) -> None:
        self._store = store

    async def bind(
        self,
        address: Address[Any],
        signal: str,
        callback: SignalCallback[T],
        *,
        id: str | None = None,
    ) -> Response[SignalBinding[T]]:
        await self._store.ready()

        return await self._store.client.bind_signal(
            address=address,
            signal=signal,
            callback=callback,
            id=id,
        )

    async def emit(
        self,
        address: Address[Any],
        signal: str,
        data: Any = None,
        *,
        id: str | None = None,
    ) -> Response[bool]:
        await self._store.ready()

        return await self._store.client.emit_signal(
            address=address,
            signal=signal,
            data=data,
            id=id,
        )
