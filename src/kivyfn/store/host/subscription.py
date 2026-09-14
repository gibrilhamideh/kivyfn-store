# src/kivyfn/store/host/subscription.py
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeAlias
from uuid import uuid4

from kivyfn.store.error import InvalidOperationError, StoreStateError
from kivyfn.store.events import (
    InsertEvent,
    RemoveEvent,
    SetEvent,
    SetRowEvent,
    StoreCommit,
)
from kivyfn.store.host.query import HostQueries, HostQuery
from kivyfn.store.protocol.message.event import (
    EventAction,
    EventMessage,
    InsertEventAction,
    RemoveEventAction,
    SetEventAction,
    SetRowEventAction,
    SubscriptionEvent,
)
from kivyfn.store.protocol.message.requests import (
    CollectionSubscriptionRequest,
    QuerySubscriptionRequest,
    SingleSubscriptionRequest,
)
from kivyfn.store.protocol.message.responses import (
    CollectionSubscriptionResponse,
    QuerySubscriptionResponse,
    SingleSubscriptionResponse,
)
from kivyfn.store.query import Query, QueryRow
from kivyfn.store.schema import ManySchema, SingleSchema


if TYPE_CHECKING:
    from kivyfn.store.store import Store


SubscriptionKey: TypeAlias = str | Query[Any]


@dataclass(slots=True)
class HostSubscription:
    """One subscription owned by one connected client."""

    subscription_id: str
    client_id: str
    source: str | HostQuery

    @property
    def key(self) -> SubscriptionKey:
        if isinstance(self.source, str):
            return self.source

        return self.source.source

    @property
    def dependencies(self) -> frozenset[str]:
        if isinstance(self.source, str):
            return frozenset({self.source})

        return self.source.dependencies


class HostSubscriptions:
    """Own Host subscription registration and Store event routing."""

    def __init__(self, store: Store, queries: HostQueries) -> None:
        self._store = store
        self._queries = queries

        self._by_id: dict[str, HostSubscription] = {}
        self._by_client: dict[
            str, dict[SubscriptionKey, HostSubscription]
        ] = {}
        self._by_schema: dict[str, dict[str, HostSubscription]] = {}

    # -------------------------------------------------
    # Subscription
    # -------------------------------------------------

    def subscribe_schema(
        self,
        client_id: str,
        request: SingleSubscriptionRequest | CollectionSubscriptionRequest,
    ) -> SingleSubscriptionResponse | CollectionSubscriptionResponse:
        node = self._store.get_node_by_schema(request.schema)

        single = isinstance(request, SingleSubscriptionRequest)
        schema_type = SingleSchema if single else ManySchema

        if not isinstance(node.schema, schema_type):
            raise InvalidOperationError(
                f"{request.schema} is not a {schema_type.__name__}"
            )

        revision, snapshot = self._store.snapshot_if_changed(
            request.schema,
            revision=request.revision,
            store_instance_id=request.store_instance_id,
        )

        subscription, _ = self._acquire(client_id, request.schema)

        if single:
            return SingleSubscriptionResponse(
                subscription_id=subscription.subscription_id,
                store_instance_id=self._store.instance_id,
                revision=revision,
                values=None if snapshot is None else tuple(snapshot.items()),
            )

        rows = None

        if snapshot is not None:
            rows = tuple(
                QueryRow(row_id, row) for row_id, row in snapshot.items()
            )

        return CollectionSubscriptionResponse(
            subscription_id=subscription.subscription_id,
            store_instance_id=self._store.instance_id,
            revision=revision,
            rows=rows,
        )

    def subscribe_query(
        self, client_id: str, request: QuerySubscriptionRequest
    ) -> QuerySubscriptionResponse:
        query = self._queries.acquire(request.query)
        subscription, created = self._acquire(client_id, query)

        if not created:
            self._queries.release(query)

        revisions = query.revisions

        unchanged = (
            request.store_instance_id == self._store.instance_id
            and request.revisions == revisions
        )

        return QuerySubscriptionResponse(
            subscription_id=subscription.subscription_id,
            store_instance_id=self._store.instance_id,
            revisions=revisions,
            rows=None if unchanged else query.rows,
        )

    def release(
        self, client_id: str, subscription_id: str
    ) -> HostSubscription | None:
        subscription = self.get(client_id, subscription_id)

        if subscription is not None:
            self._remove(subscription)

        return subscription

    def remove_client(self, client_id: str) -> tuple[HostSubscription, ...]:
        subscriptions = self.for_client(client_id)

        for subscription in subscriptions:
            self._remove(subscription)

        return subscriptions

    def clear(self) -> None:
        self._by_id.clear()
        self._by_client.clear()
        self._by_schema.clear()
        self._queries.clear()

    # -------------------------------------------------
    # Lookup
    # -------------------------------------------------

    def get(
        self, client_id: str, subscription_id: str
    ) -> HostSubscription | None:
        subscription = self._by_id.get(subscription_id)

        if subscription is None or subscription.client_id != client_id:
            return None

        return subscription

    def for_client(self, client_id: str) -> tuple[HostSubscription, ...]:
        return tuple(self._by_client.get(client_id, {}).values())

    # -------------------------------------------------
    # Store commits
    # -------------------------------------------------

    def capture(
        self, commit: StoreCommit
    ) -> tuple[tuple[str, EventMessage], ...]:
        """Convert one Store commit into per-client subscription messages."""

        self._require_commit(commit)

        subscriptions = self._affected(commit)
        clients: dict[str, list[SubscriptionEvent]] = {}
        query_actions: dict[HostQuery, tuple[EventAction, ...]] = {}

        for subscription in subscriptions:
            revisions = {
                schema: revision
                for schema, revision in commit.schema_revisions.items()
                if schema in subscription.dependencies
            }

            source = subscription.source

            if isinstance(source, str):
                actions = _schema_actions(commit, source)

            else:
                actions = query_actions.get(source)

                if actions is None:
                    captured = source.capture(commit.schema_revisions)

                    if captured is None:
                        raise StoreStateError(
                            "Query did not capture its changed dependency"
                        )

                    actions = captured
                    query_actions[source] = actions

            event = SubscriptionEvent(
                subscription_id=subscription.subscription_id,
                revisions=revisions,
                actions=actions,
            )

            clients.setdefault(subscription.client_id, []).append(event)

        return tuple(
            (
                client_id,
                EventMessage(
                    store_instance_id=self._store.instance_id,
                    events=tuple(events),
                ),
            )
            for client_id, events in clients.items()
        )

    def prepare(
        self, client_id: str, message: EventMessage
    ) -> EventMessage | None:
        """Remove events for subscriptions released before delivery."""

        if message.store_instance_id != self._store.instance_id:
            raise StoreStateError(
                "Subscription message belongs to another Store"
            )

        events = tuple(
            event
            for event in message.events
            if self.get(client_id, event.subscription_id) is not None
        )

        if not events:
            return None

        return EventMessage(
            store_instance_id=message.store_instance_id, events=events
        )

    def _affected(self, commit: StoreCommit) -> tuple[HostSubscription, ...]:
        selected: dict[str, HostSubscription] = {}

        for schema in commit.schema_revisions:
            selected.update(self._by_schema.get(schema, {}))

        return tuple(selected.values())

    def _require_commit(self, commit: StoreCommit) -> None:
        if commit.server_instance_id != self._store.instance_id:
            raise StoreStateError(
                "Subscription commit belongs to another Store"
            )

        schemas = {event.schema for event in commit.events}

        if schemas != set(commit.schema_revisions):
            raise StoreStateError(
                "Store commit revisions do not match its events"
            )

    # -------------------------------------------------
    # Registry
    # -------------------------------------------------

    def _acquire(
        self, client_id: str, source: str | HostQuery
    ) -> tuple[HostSubscription, bool]:
        key = source if isinstance(source, str) else source.source
        existing = self._by_client.get(client_id, {}).get(key)

        if existing is not None:
            return existing, False

        subscription = HostSubscription(
            subscription_id=uuid4().hex, client_id=client_id, source=source
        )

        self._by_id[subscription.subscription_id] = subscription
        self._by_client.setdefault(client_id, {})[key] = subscription

        for schema in subscription.dependencies:
            self._by_schema.setdefault(schema, {})[
                subscription.subscription_id
            ] = subscription

        return subscription, True

    def _remove(self, subscription: HostSubscription) -> None:
        subscription_id = subscription.subscription_id
        client_id = subscription.client_id

        self._by_id.pop(subscription_id, None)

        sources = self._by_client.get(client_id)

        if sources is not None:
            sources.pop(subscription.key, None)

            if not sources:
                self._by_client.pop(client_id, None)

        for schema in subscription.dependencies:
            subscribers = self._by_schema.get(schema)

            if subscribers is None:
                continue

            subscribers.pop(subscription_id, None)

            if not subscribers:
                self._by_schema.pop(schema, None)

        if isinstance(subscription.source, HostQuery):
            self._queries.release(subscription.source)

    def __len__(self) -> int:
        return len(self._by_id)


def _schema_actions(
    commit: StoreCommit, schema: str
) -> tuple[EventAction, ...]:
    actions: list[EventAction] = []

    for event in commit.events:
        if event.schema != schema:
            continue

        match event:
            case SetEvent():
                actions.append(
                    SetEventAction(values=((event.key, event.new),))
                )

            case SetRowEvent():
                actions.append(
                    SetRowEventAction(
                        row_id=event.id, values=((event.key, event.new),)
                    )
                )

            case InsertEvent():
                actions.append(
                    InsertEventAction(
                        row_id=event.id, values=tuple(event.new.items())
                    )
                )

            case RemoveEvent():
                actions.append(RemoveEventAction(row_id=event.id))

    if not actions:
        raise StoreStateError(
            f"Schema {schema!r} advanced without canonical events"
        )

    return tuple(actions)
