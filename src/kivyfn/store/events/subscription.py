# kivyfn/store/events/subscription.py
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, TypeVar
from kivyfn.store.events.event import StoreEvent
from kivyfn.store.utils.tools.uuid import generate_uuid

if TYPE_CHECKING:
    from kivyfn.store.events.listener import Listener
    from kivyfn.store.node import Node

EventT_co = TypeVar(
    "EventT_co",
    bound=StoreEvent,
    covariant=True,
)

class Subscription(
    Protocol[EventT_co],
):
    """
    Public transport-independent subscription handle.

    Concrete implementations may be local or remote.
    """

    @property
    def token(self) -> str:
        ...

    @property
    def disposed(self) -> bool:
        ...


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

    __slots__ = (
        "_node",
        "_token",
        "_listener",
    )

    def __init__(
        self,
        node: Node,
        listener: Listener,
    ) -> None:
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

