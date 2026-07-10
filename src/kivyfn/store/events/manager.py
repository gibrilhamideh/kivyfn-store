from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kivyfn.store.address import Address
from kivyfn.store.constants import InstructionOpCode
from kivyfn.store.events.listener import Listener, ListenerIndex, ForeignListener
from kivyfn.store.events.subscription import EngineSubscription

if TYPE_CHECKING:
    from kivyfn.store.core import Engine
    from kivyfn.store.node import Node
    from kivyfn.store.events.event import StoreEvent


@dataclass(slots=True)
class SubscribeResult:
    subscription_token: str
    event_token: str
    room: str
    should_join_room: bool


@dataclass(slots=True)
class UnsubscribeResult:
    room: str
    should_leave_room: bool


@dataclass(slots=True)
class DispatchMessage:
    room: str
    payload: dict[str, Any]


class SubscriptionManager:
    def __init__(self, engine: Engine) -> None:

        self._engine = engine

        # -------------------------------------------------
        # Listener indexing (event matching)
        # -------------------------------------------------

        self._indexes: dict[Address, ListenerIndex] = defaultdict(ListenerIndex)
        """
        Maps node Address → ListenerIndex.

        Each ListenerIndex holds all listeners registered for that specific node.
        Used during dispatch to quickly find matching listeners for a StoreEvent.
        """

        # -------------------------------------------------
        # Subscription tracking
        # -------------------------------------------------

        self._subscriptions: dict[str, EngineSubscription] = {}
        """
        Maps subscription_token → EngineSubscription.

        Each subscription represents a single client interest in a listener.
        Used for unsubscribe, cleanup, and lifecycle management.
        """

        self._listener_subscriptions: dict[
            Listener, set[EngineSubscription]
        ] = defaultdict(set)
        """
        Maps Listener → set of EngineSubscription.

        Tracks all subscriptions that share the same listener (event stream).

        This allows:
        - deduplicating listeners
        - removing listener only when no subscriptions remain
        """

        # -------------------------------------------------
        # Per-client tracking (critical for Socket.IO rooms)
        # -------------------------------------------------

        self._sid_listener_counts: dict[
            str, dict[Listener, int]
        ] = defaultdict(lambda: defaultdict(int))
        """
        Maps sid → (Listener → reference count).

        Tracks how many times a specific client (sid) subscribed to a listener.

        Used to decide:
        - when to join a room (count transitions 0 → 1)
        - when to leave a room (count transitions 1 → 0)

        Prevents prematurely leaving a room when multiple subscriptions exist.
        """

        self._sid_subscriptions: dict[str, set[str]] = defaultdict(set)
        """
        Maps sid → set of subscription tokens.

        Tracks which subscriptions belong to each client.

        Used for:
        - fast unsubscribe validation (ownership check)
        - efficient cleanup on disconnect (remove_sid)
        """

        # -------------------------------------------------
        # Foreign-key listeners (cross-node propagation)
        # -------------------------------------------------

        self._foreign_indexes: dict[Address, list[ForeignListener]] = defaultdict(list)
        """
        Maps referenced node Address → list of ForeignListener.

        Each ForeignListener listens to changes in a referenced node and translates
        them into events on the owner node.

        Used during dispatch to propagate foreign-key changes.
        """

        self._foreign_by_subscription: dict[
            str, list[ForeignListener]
        ] = defaultdict(list)
        """
        Maps subscription_token → list of ForeignListener.

        Tracks which foreign listeners were created for a given subscription.

        Used for cleanup:
        - when unsubscribing, remove all associated foreign listeners
        - prevents memory leaks and stale propagation
        """


    # -------------------------------------------------
    # Subscribe / Unsubscribe
    # -------------------------------------------------

    def subscribe(self, sid: str, message: dict[str, Any]) -> SubscribeResult:
        """
        Register a subscription for a client.

        - Builds a Listener from the payload (address, opcode, id, key, etc.)
        - Creates an EngineSubscription and stores it by token
        - Adds the listener to the per-address index (for dispatch)
        - Tracks ownership (sid → tokens) and reference counts (sid → listener)
        - Optionally sets up foreign-key listeners

        Returns a SubscribeResult with:
        - subscription_token (for unsubscribe)
        - event_token / room (for event routing)
        - should_join_room (whether this sid must join the room)
        """

        address = Address(*message["address"])
        node = self._engine.get_node_by_address(address)

        listener = Listener(
            address=address,
            opcode=InstructionOpCode(message["opcode"]),
            id=message.get("id"),
            key=message.get("key"),
            propagate_foreignkeys=message.get("propagate_foreignkeys", False),
            resolve_foreignkeys=message.get("resolve_foreignkeys", False),
            depth=message.get("depth", 0),
        )

        self._add_listener(listener)

        subscription = EngineSubscription(node, listener)

        self._subscriptions[subscription.token] = subscription
        self._listener_subscriptions[listener].add(subscription)

        if listener.propagate_foreignkeys:
            self._subscribe_foreignkeys(
                subscription_token=subscription.token,
                owner_node=node,
                listener=listener,
            )

        should_join = self._sid_listener_counts[sid][listener] == 0
        self._sid_listener_counts[sid][listener] += 1
        self._sid_subscriptions[sid].add(subscription.token)

        return SubscribeResult(
            subscription_token=subscription.token,
            event_token=listener.room,
            room=listener.room,
            should_join_room=should_join,
        )

    def unsubscribe(self, sid: str, token: str) -> UnsubscribeResult:
        """
        Remove a client subscription.

        - Validates that the token belongs to the given sid
        - Removes the subscription from all internal registries
        - Cleans up foreign-key listeners associated with the subscription
        - Decrements the (sid → listener) reference count
        - Determines whether the client should leave the listener's room

        Returns:
            UnsubscribeResult with:
            - room: the listener room
            - should_leave_room: True if this sid has no more subscriptions
            for this listener and should leave the room
        """
        
        if token not in self._sid_subscriptions.get(sid, set()):
            raise KeyError("Subscription not found for this client")

        self._sid_subscriptions[sid].discard(token)

        subscription = self._subscriptions.pop(token)
        listener = subscription.listener

        self._remove_subscription_from_listener(subscription)
        self._remove_foreign_listeners(token)

        self._sid_listener_counts[sid][listener] -= 1
        should_leave_room = self._sid_listener_counts[sid][listener] == 0

        if should_leave_room:
            self._sid_listener_counts[sid].pop(listener, None)

        return UnsubscribeResult(
            room=listener.room,
            should_leave_room=should_leave_room,
        )

    def remove_sid(self, sid: str) -> list[str]:
        """
        Clean up all subscriptions for a disconnected client.

        - Retrieves all subscription tokens owned by the given sid
        - Removes each subscription from internal registries
        - Cleans up associated listeners and foreign-key listeners
        - Clears per-sid listener reference counts
        - Collects affected rooms (for optional transport-level cleanup)

        Returns:
            list[str]: Rooms that were associated with this sid.
            (Socket.IO usually handles room cleanup automatically on disconnect,
            but this can be used for explicit control if needed.)
        """
        rooms: list[str] = []

        tokens = list(self._sid_subscriptions.pop(sid, ()))

        for token in tokens:
            subscription = self._subscriptions.pop(token, None)
            if subscription is None:
                continue

            listener = subscription.listener

            self._remove_subscription_from_listener(subscription)
            self._remove_foreign_listeners(token)

            rooms.append(listener.room)

        self._sid_listener_counts.pop(sid, None)

        return rooms

    # -------------------------------------------------
    # Dispatch
    # -------------------------------------------------

    def dispatch(self, event: StoreEvent) -> list[DispatchMessage]:
        """
        Dispatch a StoreEvent to all matching listeners.

        Combines:
        - direct listeners on the event's node
        - foreign-key propagated listeners from referenced nodes

        Returns:
            List of DispatchMessage ready for transport emission.
        """
        messages: list[DispatchMessage] = []

        messages.extend(self._dispatch_listeners(event))
        messages.extend(self._dispatch_foreign_listeners(event))

        return messages

    def _dispatch_listeners(self, event: StoreEvent) -> list[DispatchMessage]:
        """
        Dispatch an event to direct listeners on the same node.

        - Looks up the ListenerIndex for the event's address
        - Finds matching listeners based on opcode/id/key
        - Filters out listeners with no active subscriptions
        - Produces DispatchMessage objects (room + payload)

        Returns:
            List of messages targeting rooms for matching listeners.
        """
        index = self._indexes.get(event.address)
        if index is None:
            return []

        messages: list[DispatchMessage] = []

        for listener in index.listeners(event):
            if listener not in self._listener_subscriptions:
                continue

            messages.append(
                DispatchMessage(
                    room=listener.room,
                    payload={
                        "event_token": listener.room,
                        "event": self._payload_for_listener(listener, event),
                    },
                )
            )

        return messages

    def _dispatch_foreign_listeners(self, event: StoreEvent) -> list[DispatchMessage]:
        """
        Dispatch an event through foreign-key listeners.

        - Finds ForeignListener objects watching this event's node
        - Translates the event into owner-node events
        - Re-dispatches translated events through direct listener logic

        This enables propagation of changes across foreign-key relationships.

        Returns:
            List of messages generated from translated events.
        """
        if event.opcode is not InstructionOpCode.SETROW:
            return []

        foreign_listeners = self._foreign_indexes.get(event.address)
        if not foreign_listeners:
            return []

        messages: list[DispatchMessage] = []

        for foreign_listener in tuple(foreign_listeners):
            translated_events = foreign_listener.translate(event)

            for translated in translated_events:
                messages.extend(self._dispatch_listeners(translated))

        return messages

    def _payload_for_listener(
        self,
        listener: Listener,
        event: StoreEvent,
    ) -> dict[str, Any]:
        if not listener.resolve_foreignkeys:
            return event.payload()

        if event.opcode is not InstructionOpCode.INSERT:
            return event.payload()

        node = self._engine.get_node_by_event(event)
        row = node.snapshot(id=event.id)

        if not isinstance(row, dict):
            return event.payload()

        resolved = self._engine.resolve_foreignkeys(node, row)

        return event.copy_with(
            new_value=resolved,
        ).payload()

    # -------------------------------------------------
    # Listener lifecycle
    # -------------------------------------------------

    def _add_listener(self, listener: Listener) -> None:
        self._indexes[listener.address].add(listener)

    def _remove_subscription_from_listener(
        self,
        subscription: EngineSubscription,
    ) -> None:
        """
        Remove a subscription from its listener and clean up if unused.

        - Detaches the subscription from the listener's subscription set
        - If other subscriptions still exist for this listener, no further action
        - If this was the last subscription:
            * removes the listener from the index
            * disposes the underlying subscription

        Ensures listeners are only kept while actively used.
        """
        listener = subscription.listener

        subscriptions = self._listener_subscriptions.get(listener)
        if subscriptions is None:
            return

        subscriptions.discard(subscription)
        if subscriptions:
            return

        self._listener_subscriptions.pop(listener, None)

        index = self._indexes.get(listener.address)
        if index is not None:
            index.remove(listener)

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
        Register foreign-key listeners for a subscription.

        - Only applies to SETROW listeners (row-level updates)
        - Respects depth:
            * depth == 0 → no propagation
            * depth > 0 or None → continue
        - Determines which foreign keys to follow:
            * if a specific key is provided → only that FK
            * otherwise → all FK fields on the schema
        - Creates ForeignListener(s) on referenced nodes and tracks them
        - Passes reduced depth for recursive FK propagation

        This enables change propagation across related nodes via foreign keys.
        """

        if listener.opcode is not InstructionOpCode.SETROW:
            return

        if  listener.depth == 0:
            return

        if listener.depth is not None and listener.depth < 0:
            return

        next_depth = None if listener.depth is None else listener.depth - 1

        foreignkey_fields = owner_node.schema.foreign_key_fields()

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
        Register a single foreign-key listener for a subscription.

        - Resolves the referenced node from the given foreign key
        - Creates a ForeignListener that maps changes from the referenced node
        back to the owner node
        - Stores the listener in:
            * _foreign_indexes (for dispatch)
            * _foreign_by_subscription (for cleanup)

        - If depth allows, recursively registers foreign listeners on the
        referenced node to support FK → FK propagation

        Args:
            subscription_token:
                The subscription owning this foreign listener (used for cleanup)

            owner_node:
                The node that contains the foreign key field

            listener:
                The original listener requesting FK propagation

            foreignkey:
                The field name representing the foreign key

            depth:
                Remaining recursion depth (None = unlimited)
        """
        field = owner_node.schema.get_field(foreignkey)
        referenced_schema_name = field.schema # pyright: ignore

        referenced_node = self._engine.get_node_by_schema(referenced_schema_name)

        foreign_listener = ForeignListener(
            owner=owner_node,
            referenced=referenced_node,
            foreignkey=foreignkey,
            subscriber_id=listener.id,
        )

        self._foreign_indexes[referenced_node.address].append(foreign_listener)
        self._foreign_by_subscription[subscription_token].append(foreign_listener)

        # This lets FK -> FK changes bubble too.
        if depth == 0:
            return

        nested_listener = Listener(
            address=referenced_node.address,
            opcode=InstructionOpCode.SETROW,
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

    def _remove_foreign_listeners(self, token: str) -> None:
        """
        Remove all foreign-key listeners associated with a subscription.

        - Retrieves all ForeignListener objects tied to the given subscription token
        - Removes each listener from the referenced node’s foreign index
        - Cleans up empty buckets in _foreign_indexes

        Ensures that foreign-key propagation stops when a subscription is removed,
        preventing stale listeners and memory leaks.
        """
        foreign_listeners = self._foreign_by_subscription.pop(token, [])

        for foreign_listener in foreign_listeners:
            referenced_address = foreign_listener.referenced.address
            bucket = self._foreign_indexes.get(referenced_address)

            if bucket is None:
                continue

            try:
                bucket.remove(foreign_listener)
            except ValueError:
                pass

            if not bucket:
                self._foreign_indexes.pop(referenced_address, None)
