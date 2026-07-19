# kivyfn/store/server/subscription.py
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kivyfn.store.address import Address
from kivyfn.store.events.listener import (
    ForeignListener,
    Listener,
    ListenerIndex,
)
from kivyfn.store.events.subscription import EngineSubscription
from kivyfn.store.operations import OpCode


if TYPE_CHECKING:
    from kivyfn.store.core.engine import Engine
    from kivyfn.store.events.event import StoreEvent
    from kivyfn.store.node import Node


@dataclass(frozen=True, slots=True)
class SubscribeResult:
    """
    Result returned after registering a remote subscription.

    `should_join_channel` is true only when this is the client's first
    subscription to the corresponding event channel.
    """

    subscription_token: str
    event_token: str
    channel: str
    should_join_channel: bool


@dataclass(frozen=True, slots=True)
class UnsubscribeResult:
    """
    Result returned after removing a remote subscription.

    `should_leave_channel` is true when the client no longer owns any
    subscription associated with the corresponding event channel.
    """

    channel: str
    should_leave_channel: bool


@dataclass(frozen=True, slots=True)
class DispatchMessage:
    """
    Transport-independent outgoing store event.

    The active server transport is responsible for delivering `payload` to
    every client associated with `channel`.
    """

    channel: str
    payload: dict[str, Any]


class RemoteSubscriptionManager:
    """
    Server-side subscription registry for remote store clients.

    This manager owns:

    - listener construction and indexing
    - remote subscription tokens
    - subscription ownership by client
    - per-client listener reference counts
    - foreign-key propagation listeners
    - disconnect cleanup
    - transport-independent dispatch messages

    It accepts concrete, already parsed Python values. It does not parse raw
    transport payloads and does not perform Socket.IO, Unix-socket, TCP, or
    other transport operations directly.
    """

    def __init__(
        self,
        engine: Engine,
    ) -> None:
        self._engine = engine

        # address -> listener index
        self._indexes: dict[
            Address[Any],
            ListenerIndex,
        ] = defaultdict(
            ListenerIndex,
        )

        # subscription token -> subscription
        self._subscriptions: dict[
            str,
            EngineSubscription,
        ] = {}

        # listener -> subscriptions using that listener
        self._listener_subscriptions: dict[
            Listener,
            set[EngineSubscription],
        ] = defaultdict(set)

        # client id -> listener -> reference count
        self._client_listener_counts: dict[
            str,
            dict[Listener, int],
        ] = defaultdict(
            lambda: defaultdict(int)
        )

        # client id -> subscription tokens
        self._client_subscriptions: dict[
            str,
            set[str],
        ] = defaultdict(set)

        # referenced address -> foreign listeners
        self._foreign_indexes: dict[
            Address[Any],
            list[ForeignListener],
        ] = defaultdict(list)

        # subscription token -> foreign listeners
        self._foreign_by_subscription: dict[
            str,
            list[ForeignListener],
        ] = defaultdict(list)

    # -------------------------------------------------
    # Subscribe / unsubscribe
    # -------------------------------------------------

    def subscribe(
        self,
        client_id: str,
        opcode: OpCode,
        address: Address[Any],
        *,
        id: str | None = None,
        key: str | None = None,
        depth: int | None = 0,
        resolve_foreignkeys: bool = False,
        propagate_foreignkeys: bool = False,
    ) -> SubscribeResult:
        """
        Register one store-event subscription for a remote client.

        All values must already have been parsed by the active transport.
        Store-level validation remains owned by this manager, Listener,
        EngineSubscription, and the underlying engine.

        Returns routing information that allows the transport to join the
        client to the associated event channel when necessary.
        """
        self._validate_client_id(
            client_id,
        )

        self._validate_opcode(
            opcode,
        )

        self._validate_optional_string(
            id,
            name="Subscription row ID",
        )

        self._validate_optional_string(
            key,
            name="Subscription key",
        )

        self._validate_depth(
            depth,
        )

        self._validate_boolean(
            resolve_foreignkeys,
            name="resolve_foreignkeys",
        )

        self._validate_boolean(
            propagate_foreignkeys,
            name="propagate_foreignkeys",
        )

        node = self._engine.get_node_by_address(
            address,
        )

        listener = Listener(
            address=address,
            opcode=opcode,
            id=id,
            key=key,
            propagate_foreignkeys=propagate_foreignkeys,
            resolve_foreignkeys=resolve_foreignkeys,
            depth=depth,
        )

        self._add_listener(
            listener,
        )

        subscription = EngineSubscription(
            node,
            listener,
        )

        self._subscriptions[
            subscription.token
        ] = subscription

        self._listener_subscriptions[
            listener
        ].add(
            subscription,
        )

        if listener.propagate_foreignkeys:
            self._subscribe_foreignkeys(
                subscription_token=subscription.token,
                owner_node=node,
                listener=listener,
            )

        should_join_channel = (
            self._client_listener_counts[
                client_id
            ][listener]
            == 0
        )

        self._client_listener_counts[
            client_id
        ][listener] += 1

        self._client_subscriptions[
            client_id
        ].add(
            subscription.token,
        )

        return SubscribeResult(
            subscription_token=subscription.token,
            event_token=listener.room,
            channel=listener.room,
            should_join_channel=should_join_channel,
        )

    def unsubscribe(
        self,
        client_id: str,
        token: str,
    ) -> UnsubscribeResult:
        """
        Remove one subscription owned by a remote client.

        Raises KeyError when the subscription does not exist or belongs to
        another client.

        Returns routing information that allows the transport to leave the
        associated channel when the client has no remaining subscriptions to
        it.
        """
        self._validate_client_id(
            client_id,
        )

        self._validate_token(
            token,
        )

        client_tokens = self._client_subscriptions.get(
            client_id,
        )

        if (
            client_tokens is None
            or token not in client_tokens
        ):
            raise KeyError(
                "Subscription not found for this client"
            )

        subscription = self._subscriptions.pop(
            token,
            None,
        )

        if subscription is None:
            client_tokens.discard(
                token,
            )

            if not client_tokens:
                self._client_subscriptions.pop(
                    client_id,
                    None,
                )

            raise KeyError(
                "Subscription not found for this client"
            )

        listener = subscription.listener

        client_tokens.discard(
            token,
        )

        if not client_tokens:
            self._client_subscriptions.pop(
                client_id,
                None,
            )

        self._remove_subscription_from_listener(
            subscription,
        )

        self._remove_foreign_listeners(
            token,
        )

        listener_counts = self._client_listener_counts.get(
            client_id,
        )

        if listener_counts is None:
            should_leave_channel = True
        else:
            count = listener_counts.get(
                listener,
                0,
            )

            if count <= 1:
                listener_counts.pop(
                    listener,
                    None,
                )
                should_leave_channel = True
            else:
                listener_counts[
                    listener
                ] = count - 1
                should_leave_channel = False

            if not listener_counts:
                self._client_listener_counts.pop(
                    client_id,
                    None,
                )

        return UnsubscribeResult(
            channel=listener.room,
            should_leave_channel=should_leave_channel,
        )

    def remove_client(
        self,
        client_id: str,
    ) -> tuple[str, ...]:
        """
        Remove every subscription owned by a disconnected client.

        The returned channels may be used by transports that require explicit
        channel cleanup. Socket.IO normally removes room membership itself
        during disconnect, while other transports may use the returned values.
        """
        self._validate_client_id(
            client_id,
        )

        channels: set[str] = set()

        tokens = tuple(
            self._client_subscriptions.pop(
                client_id,
                (),
            )
        )

        for token in tokens:
            subscription = self._subscriptions.pop(
                token,
                None,
            )

            if subscription is None:
                continue

            listener = subscription.listener

            channels.add(
                listener.room,
            )

            self._remove_subscription_from_listener(
                subscription,
            )

            self._remove_foreign_listeners(
                token,
            )

        self._client_listener_counts.pop(
            client_id,
            None,
        )

        return tuple(
            channels,
        )

    # -------------------------------------------------
    # Dispatch
    # -------------------------------------------------

    def dispatch(
        self,
        event: StoreEvent,
    ) -> tuple[DispatchMessage, ...]:
        """
        Create dispatch messages for all listeners matching a store event.

        Direct listeners and foreign-key propagation listeners are both
        evaluated. The active transport is responsible for delivering the
        returned messages.
        """
        messages: list[
            DispatchMessage
        ] = []

        messages.extend(
            self._dispatch_listeners(
                event,
            )
        )

        messages.extend(
            self._dispatch_foreign_listeners(
                event,
            )
        )

        return tuple(
            messages,
        )

    def _dispatch_listeners(
        self,
        event: StoreEvent,
    ) -> list[DispatchMessage]:
        """
        Create dispatch messages for direct listeners on the event's node.
        """
        index = self._indexes.get(
            event.address,
        )

        if index is None:
            return []

        messages: list[
            DispatchMessage
        ] = []

        for listener in index.listeners(
            event,
        ):
            if listener not in self._listener_subscriptions:
                continue

            messages.append(
                DispatchMessage(
                    channel=listener.room,
                    payload={
                        "event_token": listener.room,
                        "event": self._payload_for_listener(
                            listener,
                            event,
                        ),
                    },
                )
            )

        return messages

    def _dispatch_foreign_listeners(
        self,
        event: StoreEvent,
    ) -> list[DispatchMessage]:
        """
        Translate matching foreign-key changes and dispatch owner-node events.
        """
        if event.opcode is not OpCode.SETROW:
            return []

        foreign_listeners = self._foreign_indexes.get(
            event.address,
        )

        if not foreign_listeners:
            return []

        messages: list[
            DispatchMessage
        ] = []

        for foreign_listener in tuple(
            foreign_listeners,
        ):
            translated_events = foreign_listener.translate(
                event,
            )

            for translated in translated_events:
                messages.extend(
                    self._dispatch_listeners(
                        translated,
                    )
                )

        return messages

    def _payload_for_listener(
        self,
        listener: Listener,
        event: StoreEvent,
    ) -> dict[str, Any]:
        """
        Build the event payload required by one listener.

        Insert events may include resolved foreign-key values when requested by
        the listener.
        """
        if not listener.resolve_foreignkeys:
            return event.payload()

        if event.opcode is not OpCode.INSERT:
            return event.payload()

        node = self._engine.get_node_by_event(
            event,
        )

        row = node.snapshot(
            id=event.id,
        )

        if not isinstance(
            row,
            dict,
        ):
            return event.payload()

        resolved = self._engine.resolve_foreignkeys(
            node,
            row,
        )

        return event.copy_with(
            new_value=resolved,
        ).payload()

    # -------------------------------------------------
    # Listener lifecycle
    # -------------------------------------------------

    def _add_listener(
        self,
        listener: Listener,
    ) -> None:
        """
        Add a listener to its address index.
        """
        self._indexes[
            listener.address
        ].add(
            listener,
        )

    def _remove_subscription_from_listener(
        self,
        subscription: EngineSubscription,
    ) -> None:
        """
        Detach a subscription and remove its listener when no longer used.

        The underlying EngineSubscription is disposed only when the final
        subscription sharing the listener is removed.
        """
        listener = subscription.listener

        subscriptions = self._listener_subscriptions.get(
            listener,
        )

        if subscriptions is None:
            return

        subscriptions.discard(
            subscription,
        )

        if subscriptions:
            return

        self._listener_subscriptions.pop(
            listener,
            None,
        )

        index = self._indexes.get(
            listener.address,
        )

        if index is not None:
            index.remove(
                listener,
            )

            if not index:
                self._indexes.pop(
                    listener.address,
                    None,
                )

        subscription.dispose()

    # -------------------------------------------------
    # Foreign-key listeners
    # -------------------------------------------------

    def _subscribe_foreignkeys(
        self,
        *,
        subscription_token: str,
        owner_node: Node,
        listener: Listener,
    ) -> None:
        """
        Register foreign-key propagation listeners for one subscription.

        Propagation applies only to SETROW listeners. A depth of zero disables
        propagation, a positive depth limits recursion, and None allows
        unbounded recursion.
        """
        if listener.opcode is not OpCode.SETROW:
            return

        if listener.depth == 0:
            return

        if (
            listener.depth is not None
            and listener.depth < 0
        ):
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
            if listener.key in foreignkey_fields:
                self._subscribe_foreignkey(
                    subscription_token=subscription_token,
                    owner_node=owner_node,
                    listener=listener,
                    foreignkey=listener.key,
                    depth=next_depth,
                )

            return

        for foreignkey in foreignkey_fields:
            self._subscribe_foreignkey(
                subscription_token=subscription_token,
                owner_node=owner_node,
                listener=listener,
                foreignkey=foreignkey,
                depth=next_depth,
            )

    def _subscribe_foreignkey(
        self,
        *,
        subscription_token: str,
        owner_node: Node,
        listener: Listener,
        foreignkey: str,
        depth: int | None,
    ) -> None:
        """
        Register one foreign-key listener and recursively follow nested keys.
        """
        field = owner_node.schema.get_field(
            foreignkey,
        )

        referenced_schema_name = field.schema  # pyright: ignore

        referenced_node = self._engine.get_node_by_schema(
            referenced_schema_name,
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
            id=None,
            key=None,
            depth=depth,
            propagate_foreignkeys=True,
        )

        self._subscribe_foreignkeys(
            subscription_token=subscription_token,
            owner_node=referenced_node,
            listener=nested_listener,
        )

    def _remove_foreign_listeners(
        self,
        token: str,
    ) -> None:
        """
        Remove every foreign-key listener owned by a subscription.
        """
        foreign_listeners = (
            self._foreign_by_subscription.pop(
                token,
                [],
            )
        )

        for foreign_listener in foreign_listeners:
            referenced_address = (
                foreign_listener.referenced.address
            )

            bucket = self._foreign_indexes.get(
                referenced_address,
            )

            if bucket is None:
                continue

            try:
                bucket.remove(
                    foreign_listener,
                )
            except ValueError:
                pass

            if not bucket:
                self._foreign_indexes.pop(
                    referenced_address,
                    None,
                )

    # -------------------------------------------------
    # Validation
    # -------------------------------------------------

    @staticmethod
    def _validate_client_id(
        client_id: str,
    ) -> None:
        """
        Validate the transport-provided client identifier.
        """
        if (
            not isinstance(client_id, str)
            or not client_id
        ):
            raise TypeError(
                "Subscription client ID must be a non-empty string"
            )

    @staticmethod
    def _validate_opcode(
        opcode: OpCode,
    ) -> None:
        """
        Validate the concrete subscription operation code.
        """
        if not isinstance(
            opcode,
            OpCode,
        ):
            raise TypeError(
                "Subscription opcode must be an OpCode"
            )

    @staticmethod
    def _validate_optional_string(
        value: str | None,
        *,
        name: str,
    ) -> None:
        """
        Validate an optional concrete string argument.
        """
        if (
            value is not None
            and not isinstance(value, str)
        ):
            raise TypeError(
                f"{name} must be a string or None"
            )

    @staticmethod
    def _validate_depth(
        depth: int | None,
    ) -> None:
        """
        Validate the foreign-key propagation depth.
        """
        if depth is None:
            return

        if not isinstance(
            depth,
            int,
        ):
            raise TypeError(
                "Subscription depth must be an integer or None"
            )

        if depth < 0:
            raise ValueError(
                "Subscription depth must be zero, positive, or None"
            )

    @staticmethod
    def _validate_boolean(
        value: bool,
        *,
        name: str,
    ) -> None:
        """
        Validate a concrete boolean subscription option.
        """
        if not isinstance(
            value,
            bool,
        ):
            raise TypeError(
                f"{name} must be a boolean"
            )

    @staticmethod
    def _validate_token(
        token: str,
    ) -> None:
        """
        Validate a remote subscription token.
        """
        if (
            not isinstance(token, str)
            or not token
        ):
            raise TypeError(
                "Subscription token must be a non-empty string"
            )

