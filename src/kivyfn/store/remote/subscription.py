# kivyfn/store/remote/subscription.py
from __future__ import annotations

import inspect
import weakref

from collections.abc import Callable
from typing import (
    TYPE_CHECKING,
    Any,
    Generic,
    Literal,
    Protocol,
    TypeVar,
    cast,
    overload,
)

from kivyfn.logging import get_logger
from kivyfn.store.address import Address
from kivyfn.store.events.event import (
    InsertEvent,
    RemoveEvent,
    SetEvent,
    SetRowEvent,
    StoreEvent,
)
from kivyfn.store.operations import OpCode
from kivyfn.store.response import Response


if TYPE_CHECKING:
    from kivyfn.store.remote.client import RemoteStoreClient


logger = get_logger(__name__)


EventT = TypeVar(
    "EventT",
    bound=StoreEvent,
)

RowT = TypeVar("RowT")


EventCallback = Callable[
    [EventT],
    None,
]


class RemoteSubscriptionClient(Protocol):
    """
    Client operations required by the remote subscription API.

    This protocol keeps the subscription module independent from Socket.IO
    and from the concrete RemoteStoreClient implementation.
    """

    @overload
    async def subscribe(
        self,
        opcode: Literal[OpCode.SET],
        address: Address[Any],
        callback: EventCallback[SetEvent],
        *,
        id: None = None,
        key: str | None = None,
        depth: int | None = 0,
        resolve_foreignkeys: bool = False,
        propagate_foreignkeys: bool = False,
    ) -> Response[RemoteSubscription[SetEvent]]:
        ...

    @overload
    async def subscribe(
        self,
        opcode: Literal[OpCode.SETROW],
        address: Address[Any],
        callback: EventCallback[SetRowEvent],
        *,
        id: str | None = None,
        key: str | None = None,
        depth: int | None = 0,
        resolve_foreignkeys: bool = False,
        propagate_foreignkeys: bool = False,
    ) -> Response[RemoteSubscription[SetRowEvent]]:
        ...

    @overload
    async def subscribe(
        self,
        opcode: Literal[OpCode.INSERT],
        address: Address[Any],
        callback: EventCallback[InsertEvent[RowT]],
        *,
        id: None = None,
        key: None = None,
        depth: int | None = 0,
        resolve_foreignkeys: bool = False,
        propagate_foreignkeys: bool = False,
    ) -> Response[RemoteSubscription[InsertEvent[RowT]]]:
        ...

    @overload
    async def subscribe(
        self,
        opcode: Literal[OpCode.REMOVE],
        address: Address[Any],
        callback: EventCallback[RemoveEvent[RowT]],
        *,
        id: None = None,
        key: None = None,
        depth: int | None = 0,
        resolve_foreignkeys: bool = False,
        propagate_foreignkeys: bool = False,
    ) -> Response[RemoteSubscription[RemoveEvent[RowT]]]:
        ...

    async def subscribe(
        self,
        opcode: OpCode,
        address: Address[Any],
        callback: EventCallback[Any],
        *,
        id: str | None = None,
        key: str | None = None,
        depth: int | None = 0,
        resolve_foreignkeys: bool = False,
        propagate_foreignkeys: bool = False,
    ) -> Response[RemoteSubscription[Any]]:
        ...

    async def unsubscribe(
        self,
        token: str,
    ) -> Response[bool]:
        ...


class _CallbackReference(Generic[EventT]):
    """
    Holds bound methods weakly and other callables strongly.

    Bound methods are weakly referenced so a subscription does not keep its
    owning controller, adapter, screen, or widget alive indefinitely.

    Functions, lambdas, and callable objects are retained strongly.
    """

    __slots__ = (
        "_strong",
        "_weak",
    )

    def __init__(
        self,
        callback: EventCallback[EventT],
    ) -> None:
        self._strong: EventCallback[EventT] | None = None
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
    ) -> EventCallback[EventT] | None:
        if self._weak is not None:
            return cast(
                EventCallback[EventT] | None,
                self._weak(),
            )

        return self._strong


class RemoteSubscription(Generic[EventT]):
    """
    Subscription handle for a remotely hosted store.

    The remote server owns the actual subscription. This local object owns:

    - the remote subscription token;
    - the incoming event-stream token;
    - the local callback reference;
    - asynchronous disposal of the remote subscription.

    dispose() is idempotent after a successful remote unsubscribe.
    """

    __slots__ = (
        "_client_ref",
        "_token",
        "_event_token",
        "_callback",
        "_disposed",
        "__weakref__",
    )

    def __init__(
        self,
        client: RemoteSubscriptionClient,
        token: str,
        event_token: str,
        callback: EventCallback[EventT],
    ) -> None:
        self._client_ref = weakref.ref(
            client,
        )

        self._token = token
        self._event_token = event_token

        self._callback = _CallbackReference(
            callback,
        )

        self._disposed = False

    async def dispose(
        self,
    ) -> Response[bool] | None:
        """
        Remove the subscription from the remote server.

        Returns None when the subscription was already disposed or its owning
        client no longer exists.
        """
        if self._disposed:
            return None

        client = self._client_ref()

        if client is None:
            self._mark_disposed()
            return None

        result = await client.unsubscribe(
            self._token,
        )

        if result.ok:
            self._mark_disposed()

        return result

    def dispatch(
        self,
        event: EventT,
    ) -> bool:
        """
        Dispatch an incoming remote event to the local callback.

        Returns False when the callback owner no longer exists and this
        subscription should be removed by RemoteStoreClient.
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
                close = getattr(
                    result,
                    "close",
                    None,
                )

                if callable(
                    close,
                ):
                    close()

                raise TypeError(
                    "Remote store subscription callbacks must be synchronous"
                )

        except Exception:
            logger.exception(
                "Remote store subscription callback failed: "
                "token=%s event_token=%s",
                self._token,
                self._event_token,
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
            f"disposed={self._disposed}>"
        )


class RemoteSubscriptionManager:
    """
    Public asynchronous subscription API for RemoteStore.

    This manager validates API-level argument combinations and delegates remote
    registration to RemoteStoreClient. Server-side schema and row validation
    remains authoritative.
    """

    def __init__(
        self,
        client: RemoteStoreClient,
    ) -> None:
        self._client = client

    async def set(
        self,
        callback: EventCallback[SetEvent],
        address: Address[Any],
        key: str | None = None,
    ) -> Response[RemoteSubscription[SetEvent]]:
        return await self._client.subscribe(
            opcode=OpCode.SET,
            address=address,
            callback=callback,
            key=key,
        )

    async def setrow(
        self,
        callback: EventCallback[SetRowEvent],
        address: Address[Any],
        id: str | None = None,
        key: str | None = None,
        depth: int | None = 0,
        propagate_foreignkeys: bool = False,
    ) -> Response[RemoteSubscription[SetRowEvent]]:
        self._validate_depth(
            depth,
        )

        return await self._client.subscribe(
            opcode=OpCode.SETROW,
            address=address,
            callback=callback,
            id=id,
            key=key,
            depth=depth,
            propagate_foreignkeys=propagate_foreignkeys,
        )

    async def insert(
        self,
        callback: EventCallback[InsertEvent[RowT]],
        address: Address[Any],
        resolve_foreignkeys: bool = False,
    ) -> Response[RemoteSubscription[InsertEvent[RowT]]]:
        return await self._client.subscribe(
            opcode=OpCode.INSERT,
            address=address,
            callback=callback,
            resolve_foreignkeys=resolve_foreignkeys,
        )

    async def remove(
        self,
        callback: EventCallback[RemoveEvent[RowT]],
        address: Address[Any],
    ) -> Response[RemoteSubscription[RemoveEvent[RowT]]]:
        return await self._client.subscribe(
            opcode=OpCode.REMOVE,
            address=address,
            callback=callback,
        )

    @staticmethod
    def _validate_depth(
        depth: int | None,
    ) -> None:
        if (
            depth is not None
            and depth < 0
        ):
            raise ValueError(
                "Subscription depth must be zero, positive, or None"
            )

