# kivyfn/store/local/subscription.py
from __future__ import annotations

import inspect
import weakref
from collections import defaultdict
from collections.abc import Callable, Iterable
from threading import RLock
from typing import (
    TYPE_CHECKING,
    Any,
    Generic,
    TypeVar,
    cast,
)

from kivyfn.logging import get_logger
from kivyfn.store.address import Address
from kivyfn.store.operations import OpCode
from kivyfn.store.events.event import (
    InsertEvent,
    RemoveEvent,
    SetEvent,
    SetRowEvent,
    StoreEvent,
)
from kivyfn.store.events.listener import (
    ForeignListener,
    Listener,
    ListenerIndex,
)
from kivyfn.store.utils.tools.uuid import generate_uuid


if TYPE_CHECKING:
    from kivyfn.store.core.engine import Engine
    from kivyfn.store.node import Node


logger = get_logger(__name__)


EventT = TypeVar("EventT", bound=StoreEvent)
RowT = TypeVar("RowT")


EventCallback = Callable[[EventT], None]
ForeignEdge = tuple[Address[Any], str, Address[Any]]


class _CallbackReference(Generic[EventT]):
    """
    Holds bound methods weakly and other callables strongly.

    Weakly holding bound methods prevents a subscription from keeping an
    owning screen, controller, or adapter alive indefinitely. Functions and
    callable objects are retained strongly so inline functions and lambdas do
    not disappear immediately after subscription.
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


class LocalSubscription(Generic[EventT]):
    """
    Synchronous subscription handle owned by LocalSubscriptionManager.

    Calling dispose() is idempotent. Bound-method callbacks are automatically
    removed when their owning object is garbage-collected.
    """

    __slots__ = (
        "_manager_ref",
        "_token",
        "_listener",
        "_callback",
        "_disposed",
    )

    def __init__(
        self,
        manager: LocalSubscriptionManager,
        listener: Listener,
        callback: EventCallback[EventT],
    ) -> None:
        self._manager_ref = weakref.ref(
            manager,
        )
        self._token = generate_uuid()
        self._listener = listener
        self._callback = _CallbackReference(
            callback,
        )
        self._disposed = False

    def dispose(self) -> None:
        if self._disposed:
            return

        manager = self._manager_ref()

        if manager is None:
            self._mark_disposed()
            return

        manager.unsubscribe(
            self._token,
        )

    def dispatch(
        self,
        event: EventT,
    ) -> bool:
        """
        Dispatch an event to the callback.

        Returns False when the callback no longer exists and the subscription
        should be removed.
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

            if inspect.isawaitable(result):
                close = getattr(
                    result,
                    "close",
                    None,
                )

                if callable(close):
                    close()

                raise TypeError(
                    "LocalStore subscription callbacks must be synchronous"
                )

        except Exception:
            logger.exception(
                "Local store subscription callback failed: token=%s",
                self._token,
            )

        return True

    def _mark_disposed(self) -> None:
        self._disposed = True

    @property
    def token(self) -> str:
        return self._token

    @property
    def listener(self) -> Listener:
        return self._listener

    @property
    def disposed(self) -> bool:
        return self._disposed

    def __repr__(self) -> str:
        return (
            f"<{type(self).__name__} "
            f"token={self._token!r} "
            f"listener={self._listener!r} "
            f"disposed={self._disposed}>"
        )


class LocalSubscriptionManager:
    """
    Synchronous in-process subscription manager.

    Responsibilities:
    - register typed local subscriptions
    - index listeners for efficient event matching
    - propagate foreign-key changes
    - resolve foreign keys for insert subscriptions when requested
    - clean up subscriptions and dead bound-method callbacks

    It contains no client IDs, transport rooms, serialized messages, asyncio,
    or networking concerns.
    """

    def __init__(
        self,
        engine: Engine,
    ) -> None:
        self._engine = engine
        self._lock = RLock()

        # Address -> listener matching index.
        self._indexes: dict[
            Address[Any],
            ListenerIndex,
        ] = {}

        # Address -> number of distinct listeners indexed for that address.
        self._address_listener_counts: dict[
            Address[Any],
            int,
        ] = defaultdict(int)

        # Subscription token -> subscription.
        self._subscriptions: dict[
            str,
            LocalSubscription[Any],
        ] = {}

        # Listener -> subscription tokens sharing that listener.
        self._listener_subscriptions: dict[
            Listener,
            set[str],
        ] = defaultdict(set)

        # Referenced address -> foreign listeners translating its events.
        self._foreign_indexes: dict[
            Address[Any],
            list[ForeignListener],
        ] = defaultdict(list)

        # Subscription token -> foreign listeners created for it.
        self._foreign_by_subscription: dict[
            str,
            list[ForeignListener],
        ] = defaultdict(list)

    # -------------------------------------------------
    # Public subscription API
    # -------------------------------------------------

    def set(
        self,
        callback: EventCallback[SetEvent],
        address: Address[Any],
        key: str | None = None,
    ) -> LocalSubscription[SetEvent]:
        listener = Listener(
            address=address,
            opcode=OpCode.SET,
            key=key,
        )

        return self._subscribe(
            listener,
            callback,
        )

    def setrow(
        self,
        callback: EventCallback[SetRowEvent],
        address: Address[Any],
        id: str | None = None,
        key: str | None = None,
        depth: int | None = 0,
        propagate_foreignkeys: bool = False,
    ) -> LocalSubscription[SetRowEvent]:
        self._validate_depth(
            depth,
        )

        listener = Listener(
            address=address,
            opcode=OpCode.SETROW,
            id=id,
            key=key,
            depth=depth,
            propagate_foreignkeys=propagate_foreignkeys,
        )

        return self._subscribe(
            listener,
            callback,
        )

    def insert(
        self,
        callback: EventCallback[InsertEvent[RowT]],
        address: Address[Any],
        resolve_foreignkeys: bool = False,
    ) -> LocalSubscription[InsertEvent[RowT]]:
        listener = Listener(
            address=address,
            opcode=OpCode.INSERT,
            resolve_foreignkeys=resolve_foreignkeys,
        )

        return self._subscribe(
            listener,
            callback,
        )

    def remove(
        self,
        callback: EventCallback[RemoveEvent[RowT]],
        address: Address[Any],
    ) -> LocalSubscription[RemoveEvent[RowT]]:
        listener = Listener(
            address=address,
            opcode=OpCode.REMOVE,
        )

        return self._subscribe(
            listener,
            callback,
        )

    def unsubscribe(
        self,
        token: str,
    ) -> bool:
        """
        Remove a subscription by token.

        Returns False when the token was already absent.
        """
        with self._lock:
            subscription = self._subscriptions.pop(
                token,
                None,
            )

            if subscription is None:
                return False

            listener = subscription.listener
            tokens = self._listener_subscriptions.get(
                listener,
            )

            if tokens is not None:
                tokens.discard(
                    token,
                )

                if not tokens:
                    self._listener_subscriptions.pop(
                        listener,
                        None,
                    )
                    self._remove_listener(
                        listener,
                    )

            self._remove_foreign_listeners(
                token,
            )
            subscription._mark_disposed()

            return True

    def clear(self) -> None:
        """
        Dispose every subscription and release all indexes.
        """
        with self._lock:
            subscriptions = tuple(
                self._subscriptions.values(),
            )

            self._subscriptions.clear()
            self._listener_subscriptions.clear()
            self._indexes.clear()
            self._address_listener_counts.clear()
            self._foreign_indexes.clear()
            self._foreign_by_subscription.clear()

            for subscription in subscriptions:
                subscription._mark_disposed()

    # -------------------------------------------------
    # Dispatch
    # -------------------------------------------------

    def dispatch(
        self,
        event: StoreEvent,
    ) -> None:
        self.dispatch_many(
            (event,),
        )

    def dispatch_many(
        self,
        events: Iterable[StoreEvent],
    ) -> None:
        """
        Dispatch store events synchronously.

        Manager locks are released before user callbacks execute. This allows
        callbacks to mutate the store, subscribe, or dispose subscriptions
        without deadlocking the subscription registry.
        """
        deliveries: list[
            tuple[LocalSubscription[Any], StoreEvent]
        ] = []

        with self._lock:
            for event in events:
                self._collect_event_deliveries(
                    event=event,
                    deliveries=deliveries,
                    propagation_path=set(),
                )

        dead_tokens: set[str] = set()

        for subscription, event in deliveries:
            if not subscription.dispatch(
                event,
            ):
                dead_tokens.add(
                    subscription.token,
                )

        for token in dead_tokens:
            self.unsubscribe(
                token,
            )

    # -------------------------------------------------
    # Registration
    # -------------------------------------------------

    def _subscribe(
        self,
        listener: Listener,
        callback: EventCallback[EventT],
    ) -> LocalSubscription[EventT]:
        self._validate_listener(
            listener,
        )

        subscription = LocalSubscription(
            manager=self,
            listener=listener,
            callback=callback,
        )

        with self._lock:
            tokens = self._listener_subscriptions[
                listener
            ]

            if not tokens:
                self._add_listener(
                    listener,
                )

            tokens.add(
                subscription.token,
            )

            self._subscriptions[
                subscription.token
            ] = subscription

            if listener.propagate_foreignkeys:
                self._subscribe_foreignkeys(
                    subscription_token=subscription.token,
                    owner_node=self._engine.get_node_by_address(
                        listener.address,
                    ),
                    listener=listener,
                    visited=set(),
                )

        return subscription

    def _validate_listener(
        self,
        listener: Listener,
    ) -> None:
        node = self._engine.get_node_by_address(
            listener.address,
        )

        opcode = listener.opcode

        if opcode is OpCode.SET:
            if node.schema.many:
                raise ValueError(
                    f"SET subscriptions require a many=False schema: "
                    f"{node.schema.name}"
                )

            if listener.id is not None:
                raise ValueError(
                    "SET subscriptions do not accept a row id"
                )

            self._validate_field(
                node,
                listener.key,
            )
            return

        if opcode is OpCode.SETROW:
            if not node.schema.many:
                raise ValueError(
                    f"SETROW subscriptions require a many=True schema: "
                    f"{node.schema.name}"
                )

            if (
                listener.id is not None
                and not node.exists(listener.id)
            ):
                raise ValueError(
                    f"Row {listener.id!r} does not exist in "
                    f"{node.schema.name}"
                )

            self._validate_field(
                node,
                listener.key,
            )
            return

        if opcode in (
            OpCode.INSERT,
            OpCode.REMOVE,
        ):
            if not node.schema.many:
                raise ValueError(
                    f"{opcode.value.upper()} subscriptions "
                    f"require a many=True schema: "
                    f"{node.schema.name}"
                )

            if listener.id is not None or listener.key is not None:
                raise ValueError(
                    f"{opcode.value.upper()} subscriptions "
                    "do not accept id or key filters"
                )

            return

        raise ValueError(
            f"Unsupported subscription opcode: {opcode!r}"
        )


    @staticmethod
    def _validate_field(
        node: Node,
        field: str | None,
    ) -> None:
        if field is not None:
            node.schema.get_field(
                field,
            )


    @staticmethod
    def _validate_depth(
        depth: int | None,
    ) -> None:
        if depth is not None and depth < 0:
            raise ValueError(
                "Subscription depth must be zero, positive, or None"
            )

    # -------------------------------------------------
    # Direct listener indexing
    # -------------------------------------------------

    def _add_listener(
        self,
        listener: Listener,
    ) -> None:
        index = self._indexes.get(
            listener.address,
        )

        if index is None:
            index = ListenerIndex()
            self._indexes[
                listener.address
            ] = index

        index.add(
            listener,
        )

        self._address_listener_counts[
            listener.address
        ] += 1

    def _remove_listener(
        self,
        listener: Listener,
    ) -> None:
        index = self._indexes.get(
            listener.address,
        )

        if index is None:
            return

        index.remove(
            listener,
        )

        remaining = (
            self._address_listener_counts[
                listener.address
            ]
            - 1
        )

        if remaining > 0:
            self._address_listener_counts[
                listener.address
            ] = remaining
            return

        self._address_listener_counts.pop(
            listener.address,
            None,
        )
        self._indexes.pop(
            listener.address,
            None,
        )

    # -------------------------------------------------
    # Delivery collection
    # -------------------------------------------------

    def _collect_event_deliveries(
        self,
        *,
        event: StoreEvent,
        deliveries: list[
            tuple[LocalSubscription[Any], StoreEvent]
        ],
        propagation_path: set[
            tuple[Address[Any], str, str]
        ],
    ) -> None:
        self._collect_direct_deliveries(
            event,
            deliveries,
        )

        if event.opcode is not OpCode.SETROW:
            return

        foreign_listeners = self._foreign_indexes.get(
            event.address,
        )

        if not foreign_listeners:
            return

        for foreign_listener in tuple(
            foreign_listeners,
        ):
            path_key = (
                event.address,
                foreign_listener.owner.schema.name,
                foreign_listener.foreignkey,
            )

            if path_key in propagation_path:
                continue

            nested_path = set(
                propagation_path,
            )
            nested_path.add(
                path_key,
            )

            for translated in foreign_listener.translate(
                event,
            ):
                self._collect_event_deliveries(
                    event=translated,
                    deliveries=deliveries,
                    propagation_path=nested_path,
                )

    def _collect_direct_deliveries(
        self,
        event: StoreEvent,
        deliveries: list[
            tuple[LocalSubscription[Any], StoreEvent]
        ],
    ) -> None:
        index = self._indexes.get(
            event.address,
        )

        if index is None:
            return

        for listener in index.listeners(
            event,
        ):
            tokens = self._listener_subscriptions.get(
                listener,
            )

            if not tokens:
                continue

            dispatched_event = self._event_for_listener(
                listener,
                event,
            )

            for token in tuple(
                tokens,
            ):
                subscription = self._subscriptions.get(
                    token,
                )

                if subscription is None:
                    continue

                deliveries.append(
                    (
                        subscription,
                        dispatched_event,
                    )
                )

    def _event_for_listener(
        self,
        listener: Listener,
        event: StoreEvent,
    ) -> StoreEvent:
        if not listener.resolve_foreignkeys:
            return event

        if event.opcode is not OpCode.INSERT:
            return event

        node = self._engine.get_node_by_event(
            event,
        )

        row = node.snapshot(
            id=event.id,
        )

        if not isinstance(row, dict):
            return event

        return event.copy_with(
            new_value=self._engine.resolve_foreignkeys(
                node,
                row,
            ),
        )

    # -------------------------------------------------
    # Foreign-key propagation
    # -------------------------------------------------

    def _subscribe_foreignkeys(
        self,
        *,
        subscription_token: str,
        owner_node: Node,
        listener: Listener,
        visited: set[ForeignEdge],
    ) -> None:
        if listener.opcode is not OpCode.SETROW:
            return

        if listener.depth == 0:
            return

        next_depth = (
            None
            if listener.depth is None
            else listener.depth - 1
        )

        foreignkey_fields = (
            owner_node.schema.foreign_key_fields()
        )

        if listener.key is not None:
            field = foreignkey_fields.get(
                listener.key,
            )

            if field is None:
                return

            self._subscribe_foreignkey(
                subscription_token=subscription_token,
                owner_node=owner_node,
                listener=listener,
                foreignkey=listener.key,
                referenced_schema=field.schema,
                depth=next_depth,
                visited=visited,
            )
            return

        for foreignkey, field in foreignkey_fields.items():
            self._subscribe_foreignkey(
                subscription_token=subscription_token,
                owner_node=owner_node,
                listener=listener,
                foreignkey=foreignkey,
                referenced_schema=field.schema,
                depth=next_depth,
                visited=visited,
            )

    def _subscribe_foreignkey(
        self,
        *,
        subscription_token: str,
        owner_node: Node,
        listener: Listener,
        foreignkey: str,
        referenced_schema: str,
        depth: int | None,
        visited: set[ForeignEdge],
    ) -> None:
        referenced_node = self._engine.get_node_by_schema(
            referenced_schema,
        )

        edge: ForeignEdge = (
            owner_node.address,
            foreignkey,
            referenced_node.address,
        )

        if edge in visited:
            return

        visited.add(
            edge,
        )

        foreign_listener = ForeignListener(
            owner=owner_node,
            referenced=referenced_node,
            foreignkey=foreignkey,
            subscriber_id=listener.id,
        )

        self._foreign_indexes[
            referenced_node.address
        ].append(
            foreign_listener,
        )

        self._foreign_by_subscription[
            subscription_token
        ].append(
            foreign_listener,
        )

        if depth == 0:
            return

        nested_listener = Listener(
            address=referenced_node.address,
            opcode=OpCode.SETROW,
            depth=depth,
            propagate_foreignkeys=True,
        )

        self._subscribe_foreignkeys(
            subscription_token=subscription_token,
            owner_node=referenced_node,
            listener=nested_listener,
            visited=visited,
        )

    def _remove_foreign_listeners(
        self,
        token: str,
    ) -> None:
        foreign_listeners = self._foreign_by_subscription.pop(
            token,
            (),
        )

        for foreign_listener in foreign_listeners:
            address = foreign_listener.referenced_address
            bucket = self._foreign_indexes.get(
                address,
            )

            if bucket is None:
                continue

            try:
                bucket.remove(
                    foreign_listener,
                )
            except ValueError:
                continue

            if not bucket:
                self._foreign_indexes.pop(
                    address,
                    None,
                )

    # -------------------------------------------------
    # State
    # -------------------------------------------------

    def __len__(self) -> int:
        with self._lock:
            return len(
                self._subscriptions,
            )

    def __bool__(self) -> bool:
        return len(self) > 0
