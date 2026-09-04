from __future__ import annotations

import inspect
import weakref

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import (
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


@dataclass(frozen=True, slots=True)
class SubscriptionSpec:
    """Connection-independent description of a subscription."""

    opcode: OpCode
    address: Address[Any]
    id: str | None = None
    key: str | None = None
    depth: int | None = 0
    resolve_foreignkeys: bool = False
    propagate_foreignkeys: bool = False

    def payload(self) -> dict[str, Any]:
        return {
            "address": self.address.to_list(),
            "opcode": self.opcode.value,
            "id": self.id,
            "key": self.key,
            "depth": self.depth,
            "resolve_foreignkeys": self.resolve_foreignkeys,
            "propagate_foreignkeys": self.propagate_foreignkeys,
        }


class DispatchResult(str, Enum):
    """Outcome of dispatching one event to a local callback."""

    APPLIED = "applied"
    CALLBACK_GONE = "callback_gone"
    FAILED = "failed"


class RemoteSubscriptionClient(Protocol):
    """Client operations required by the subscription API."""

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

    async def dispose_subscription(
        self,
        subscription: RemoteSubscription[Any],
    ) -> Response[bool] | None:
        ...


class _CallbackReference(Generic[EventT]):
    """Hold bound methods weakly and other callables strongly."""

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

        if inspect.ismethod(callback) and callback.__self__ is not None:
            self._weak = weakref.WeakMethod(
                callback,
            )
            return

        self._strong = callback

    def get(self) -> EventCallback[EventT] | None:
        if self._weak is not None:
            return cast(
                EventCallback[EventT] | None,
                self._weak(),
            )

        return self._strong


class RemoteSubscription(Generic[EventT]):
    """Durable local handle for a reconnectable subscription."""

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
        client: RemoteSubscriptionClient,
        token: str,
        event_token: str,
        spec: SubscriptionSpec,
        callback: EventCallback[EventT],
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

        return await client.dispose_subscription(
            self,
        )

    def dispatch(
        self,
        event: EventT,
    ) -> DispatchResult:
        if self._disposed:
            return DispatchResult.CALLBACK_GONE

        callback = self._callback.get()

        if callback is None:
            return DispatchResult.CALLBACK_GONE

        try:
            result = callback(
                event,
            )

            if inspect.isawaitable(result):
                close = getattr(
                    result,
                    "close",
                    None,
                )

                if callable(close):
                    close()

                raise TypeError(
                    "Remote store subscription callbacks must be "
                    "synchronous"
                )

        except Exception:
            logger.exception(
                "Remote store subscription callback failed: "
                "token=%s event_token=%s address=%s",
                self._token,
                self._event_token,
                self.address,
            )
            return DispatchResult.FAILED

        return DispatchResult.APPLIED

    def _replace_remote(
        self,
        token: str,
        event_token: str,
    ) -> None:
        if self._disposed:
            raise RuntimeError(
                "Cannot restore a disposed subscription"
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
    def spec(self) -> SubscriptionSpec:
        return self._spec

    @property
    def address(self) -> Address[Any]:
        return self._spec.address

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
            f"disposed={self._disposed}>"
        )


class RemoteSubscriptionManager:
    """Public asynchronous subscription API for RemoteStore."""

    def __init__(
        self,
        client: RemoteSubscriptionClient,
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
        if depth is not None and depth < 0:
            raise ValueError(
                "Subscription depth must be zero, positive, or None"
            )
