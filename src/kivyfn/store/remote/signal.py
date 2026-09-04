from __future__ import annotations

import inspect
import weakref

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Any,
    Generic,
    TypeAlias,
    TypeVar,
    cast,
)

from kivyfn.logging import get_logger
from kivyfn.store.address import Address
from kivyfn.store.response import Response
from kivyfn.store.signals.event import SignalEvent


if TYPE_CHECKING:
    from kivyfn.store.remote.client import RemoteStoreClient


logger = get_logger(__name__)


T = TypeVar("T")


SignalCallback: TypeAlias = Callable[
    [SignalEvent[T]],
    None | Awaitable[None],
]


@dataclass(frozen=True, slots=True)
class SignalBindingSpec:
    """Connection-independent description of a signal binding."""

    address: Address[Any]
    signal: str
    id: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "address": self.address.to_list(),
            "signal": self.signal,
            "id": self.id,
        }


class _CallbackReference(Generic[T]):
    """Hold bound methods weakly and other callables strongly."""

    __slots__ = (
        "_strong",
        "_weak",
    )

    def __init__(
        self,
        callback: SignalCallback[T],
    ) -> None:
        self._strong: SignalCallback[T] | None = None
        self._weak: weakref.WeakMethod[Any] | None = None

        if inspect.ismethod(callback) and callback.__self__ is not None:
            self._weak = weakref.WeakMethod(
                callback,
            )
            return

        self._strong = callback

    def get(self) -> SignalCallback[T] | None:
        if self._weak is not None:
            return cast(
                SignalCallback[T] | None,
                self._weak(),
            )

        return self._strong


class RemoteSignalBinding(Generic[T]):
    """Durable local handle for a reconnectable signal binding."""

    __slots__ = (
        "_client_ref",
        "_token",
        "_event_token",
        "_spec",
        "_callback",
        "_disposed",
        "__weakref__",
    )

    def __init__(
        self,
        client: RemoteStoreClient,
        token: str,
        event_token: str,
        spec: SignalBindingSpec,
        callback: SignalCallback[T],
    ) -> None:
        self._client_ref = weakref.ref(
            client,
        )
        self._token = token
        self._event_token = event_token
        self._spec = spec
        self._callback = _CallbackReference(
            callback,
        )
        self._disposed = False

    async def dispose(self) -> Response[bool] | None:
        if self._disposed:
            return None

        client = self._client_ref()

        if client is None:
            self._mark_disposed()
            return None

        return await client.dispose_signal_binding(
            self,
        )

    async def dispatch(
        self,
        event: SignalEvent[T],
    ) -> bool:
        if self._disposed:
            return False

        callback = self._callback.get()

        if callback is None:
            return False

        try:
            result = callback(
                event,
            )

            if inspect.isawaitable(result):
                await result

        except Exception:
            logger.exception(
                "Remote signal callback failed: "
                "token=%s event_token=%s signal=%s",
                self._token,
                self._event_token,
                self.signal,
            )

        return True

    def _replace_remote(
        self,
        token: str,
        event_token: str,
    ) -> None:
        if self._disposed:
            raise RuntimeError(
                "Cannot restore a disposed signal binding"
            )

        self._token = token
        self._event_token = event_token

    def _mark_disposed(self) -> None:
        self._disposed = True

    @property
    def token(self) -> str:
        return self._token

    @property
    def event_token(self) -> str:
        return self._event_token

    @property
    def spec(self) -> SignalBindingSpec:
        return self._spec

    @property
    def address(self) -> Address[Any]:
        return self._spec.address

    @property
    def signal(self) -> str:
        return self._spec.signal

    @property
    def id(self) -> str | None:
        return self._spec.id

    @property
    def callback_alive(self) -> bool:
        return self._callback.get() is not None

    @property
    def disposed(self) -> bool:
        return self._disposed

    def __repr__(self) -> str:
        return (
            f"<{type(self).__name__} "
            f"token={self._token!r} "
            f"event_token={self._event_token!r} "
            f"address={self.address!r} "
            f"signal={self.signal!r} "
            f"id={self.id!r} "
            f"disposed={self._disposed}>"
        )


class RemoteSignalManager:
    """Public asynchronous signal API for RemoteStore."""

    def __init__(
        self,
        client: RemoteStoreClient,
    ) -> None:
        self._client = client

    async def bind(
        self,
        address: Address[Any],
        signal: str,
        callback: SignalCallback[T],
        *,
        id: str | None = None,
    ) -> Response[RemoteSignalBinding[T]]:
        return await self._client.bind_signal(
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
        return await self._client.emit_signal(
            address=address,
            signal=signal,
            data=data,
            id=id,
        )
