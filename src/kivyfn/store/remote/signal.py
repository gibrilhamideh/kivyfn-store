# kivyfn/store/remote/signal.py
from __future__ import annotations

import inspect
import weakref
from collections.abc import Awaitable, Callable
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
from kivyfn.store.signals.event import SignalEvent
from kivyfn.store.transport import response
from kivyfn.store.transport.response import Response


if TYPE_CHECKING:
    from kivyfn.store.remote.client import RemoteStoreClient


logger = get_logger(__name__)


T = TypeVar("T")


SignalCallback: TypeAlias = Callable[
    [SignalEvent[T]],
    None | Awaitable[None],
]


class _CallbackReference(Generic[T]):
    """
    Holds bound methods weakly and other callables strongly.

    Bound methods are weakly referenced so a signal binding does not keep its
    owning controller, adapter, screen, or widget alive.

    Functions, lambdas, and callable objects are retained strongly.
    """

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

        if (
            inspect.ismethod(callback)
            and callback.__self__ is not None
        ):
            self._weak = weakref.WeakMethod(
                callback,
            )
            return

        self._strong = callback

    def get(
        self,
    ) -> SignalCallback[T] | None:
        if self._weak is not None:
            return cast(
                SignalCallback[T] | None,
                self._weak(),
            )

        return self._strong


class RemoteSignalBinding(Generic[T]):
    __slots__ = (
        "_client_ref",
        "_token",
        "_event_token",
        "_address",
        "_signal",
        "_id",
        "_callback",
        "_disposed",
        "__weakref__",
    )

    def __init__(
        self,
        client: RemoteStoreClient,
        token: str,
        event_token: str,
        address: Address[Any],
        signal: str,
        callback: SignalCallback[T],
        *,
        id: str | None,
    ) -> None:
        self._client_ref = weakref.ref(
            client,
        )

        self._token = token
        self._event_token = event_token
        self._address = address
        self._signal = signal
        self._id = id
        self._callback = _CallbackReference(
            callback,
        )
        self._disposed = False

    async def dispose(
        self,
    ) -> Response[bool] | None:
        """
        Remove the binding from the remote server.

        Returns None when the binding was already disposed or the owning client
        no longer exists.
        """
        if self._disposed:
            return None

        client = self._client_ref()

        if client is None:
            self._mark_disposed()
            return None

        result = await client.request_signal_unbind(
            self._token,
        )

        if result.ok is not False:
            self._mark_disposed()

        return result

    async def dispatch(
        self,
        event: SignalEvent[T],
    ) -> bool:
        """
        Dispatch an incoming signal event.

        Both synchronous and asynchronous callbacks are supported.

        Returns False when the callback owner no longer exists and the client
        should remove the remote binding.
        """
        if self._disposed:
            return False

        callback = self._callback.get()

        if callback is None:
            return False

        try:
            result = callback(
                event,
            )

            if inspect.isawaitable(
                result,
            ):
                await result

        except Exception:
            logger.exception(
                "Remote store signal callback failed: "
                "token=%s event_token=%s signal=%s",
                self._token,
                self._event_token,
                self._signal,
            )

        return True

    def _mark_disposed(
        self,
    ) -> None:
        self._disposed = True

    @property
    def token(
        self,
    ) -> str:
        return self._token

    @property
    def event_token(
        self,
    ) -> str:
        return self._event_token

    @property
    def address(
        self,
    ) -> Address[Any]:
        return self._address

    @property
    def signal(
        self,
    ) -> str:
        return self._signal

    @property
    def id(
        self,
    ) -> str | None:
        return self._id

    @property
    def disposed(
        self,
    ) -> bool:
        return self._disposed

    def __repr__(
        self,
    ) -> str:
        return (
            f"<{type(self).__name__} "
            f"token={self._token!r} "
            f"event_token={self._event_token!r} "
            f"address={self._address!r} "
            f"signal={self._signal!r} "
            f"id={self._id!r} "
            f"disposed={self._disposed}>"
        )


class RemoteSignalManager:
    """
    Public asynchronous signal API for RemoteStore.

    The remote server remains authoritative for schema, signal, and row-scope
    validation.
    """

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
        result = await self._client.request_signal_bind(
            address=address,
            signal=signal,
            id=id,
        )

        if result.ok is False:
            return response.failure(
                status=result.status,
                error=result.error,
            )

        data = result.data

        if not isinstance(
            data,
            dict,
        ):
            raise RuntimeError(
                "Remote signal-bind response must contain an object"
            )

        binding_token = data.get(
            "binding_token",
        )

        event_token = data.get(
            "event_token",
        )

        if not isinstance(
            binding_token,
            str,
        ):
            raise RuntimeError(
                "Remote signal-bind response is missing binding_token"
            )

        if not isinstance(
            event_token,
            str,
        ):
            raise RuntimeError(
                "Remote signal-bind response is missing event_token"
            )

        binding: RemoteSignalBinding[T] = RemoteSignalBinding(
            client=self._client,
            token=binding_token,
            event_token=event_token,
            address=address,
            signal=signal,
            callback=callback,
            id=id,
        )

        self._client.register_signal_binding(
            binding,
        )

        return response.success(
            data=binding,
            status=result.status,
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
