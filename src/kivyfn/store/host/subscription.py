# src/kivyfn/store/host/subscription.py
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, TypeAlias, cast
from uuid import uuid4

from kivyfn.store.error import InvalidOperationError
from kivyfn.store.events import StoreCommit
from kivyfn.store.host.query import HostQueries, HostQuery
from kivyfn.store.host.revision import SubscriptionRevision
from kivyfn.store.protocol.message.event import EventMessage, QueryEvent
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


@dataclass(frozen=True, slots=True)
class HostSubscription:
    """One owned subscription with its shared source and delivery cursor."""

    subscription_id: str
    client_id: str
    source: str | HostQuery
    revision: SubscriptionRevision

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
    """Own subscription snapshots, dependency indexes, and event selection.

    Codec validation is complete before requests arrive here. Schema-kind and
    live-query rules are checked while resolving the subscription. Store's
    public synchronization scope keeps snapshots and their revisions together.
    """

    def __init__(self, store: Store, queries: HostQueries) -> None:
        self._store = store
        self._queries = queries
        self._by_id: dict[str, HostSubscription] = {}
        self._by_client: dict[
            str, dict[SubscriptionKey, HostSubscription]
        ] = {}
        self._by_schema: dict[str, dict[str, HostSubscription]] = {}

    def subscribe_schema(
        self,
        client_id: str,
        request: SingleSubscriptionRequest | CollectionSubscriptionRequest,
    ) -> SingleSubscriptionResponse | CollectionSubscriptionResponse:
        with self._store.synchronized():
            node = self._store.get_node_by_schema(request.schema)
            single = isinstance(request, SingleSubscriptionRequest)
            schema_type = SingleSchema if single else ManySchema

            if not isinstance(node.schema, schema_type):
                raise InvalidOperationError(
                    f"{request.schema} is not a {schema_type.__name__}"
                )

            snapshot = self._store.snapshot_by_schema(request.schema)
            revision = self._store.revision
            subscription, _ = self._acquire(
                client_id, request.schema, revision
            )

            if single:
                return SingleSubscriptionResponse(
                    request_id=request.request_id,
                    subscription_id=subscription.subscription_id,
                    store_instance_id=self._store.instance_id,
                    revision=revision,
                    values=tuple(snapshot.items()),
                )

            return CollectionSubscriptionResponse(
                request_id=request.request_id,
                subscription_id=subscription.subscription_id,
                store_instance_id=self._store.instance_id,
                revision=revision,
                rows=tuple(QueryRow(id, row) for id, row in snapshot.items()),
            )

    def subscribe_query(
        self, client_id: str, request: QuerySubscriptionRequest
    ) -> QuerySubscriptionResponse:
        with self._store.synchronized():
            query = self._queries.acquire(request.query, self._store.revision)
            subscription, created = self._acquire(
                client_id, query, query.revision
            )

            if not created:
                self._queries.release(query)

            return QuerySubscriptionResponse(
                request_id=request.request_id,
                subscription_id=subscription.subscription_id,
                store_instance_id=self._store.instance_id,
                revision=query.revision,
                shape=query.shape,
                rows=query.rows,
            )

    def get(
        self, client_id: str, subscription_id: str
    ) -> HostSubscription | None:
        """Look up a subscription owned by this client."""
        with self._store.synchronized():
            subscription = self._by_id.get(subscription_id)

            if subscription is None or subscription.client_id != client_id:
                return None

            return subscription

    def for_client(self, client_id: str) -> tuple[HostSubscription, ...]:
        with self._store.synchronized():
            return tuple(self._by_client.get(client_id, {}).values())

    def affected(self, commit: StoreCommit) -> tuple[HostSubscription, ...]:
        """Select each subscription once across all changed schema names."""
        with self._store.synchronized():
            selected: dict[str, HostSubscription] = {}

            for schema in dict.fromkeys(e.schema for e in commit.events):
                selected.update(self._by_schema.get(schema, {}))

            return tuple(selected.values())

    def capture(
        self, commit: StoreCommit
    ) -> tuple[tuple[str, EventMessage], ...]:
        """Capture all affected query results before asynchronous delivery."""
        with self._store.synchronized():
            revision = cast(int, commit.revision)
            selected: dict[str, list[HostSubscription]] = {}

            for subscription in self.affected(commit):
                if not subscription.revision.covers(revision):
                    selected.setdefault(subscription.client_id, []).append(
                        subscription
                    )

            messages: list[tuple[str, EventMessage]] = []

            for client_id, subscriptions in selected.items():
                schemas: set[str] = set()
                query_events: list[QueryEvent] = []

                for subscription in subscriptions:
                    source = subscription.source

                    if isinstance(source, str):
                        schemas.add(source)
                    else:
                        source.capture(revision)
                        event = source.event(subscription.subscription_id)

                        if event is not None:
                            query_events.append(event)

                message = EventMessage(
                    commit=replace(
                        commit,
                        events=tuple(
                            e for e in commit.events if e.schema in schemas
                        ),
                    ),
                    subscription_ids=tuple(
                        s.subscription_id for s in subscriptions
                    ),
                    query_events=tuple(query_events),
                )
                messages.append((client_id, message))

            return tuple(messages)

    def prepare(
        self, client_id: str, message: EventMessage
    ) -> EventMessage | None:
        """Filter released subscriptions and revisions covered by snapshots."""
        with self._store.synchronized():
            revision = cast(int, message.commit.revision)
            subscription_ids: list[str] = []
            schemas: set[str] = set()

            for subscription_id in message.subscription_ids:
                subscription = self.get(client_id, subscription_id)

                if subscription is None:
                    continue

                if not subscription.revision.accept(revision):
                    continue

                subscription_ids.append(subscription_id)

                if isinstance(subscription.source, str):
                    schemas.add(subscription.source)

            if not subscription_ids:
                return None

            covered = set(subscription_ids)
            return EventMessage(
                commit=replace(
                    message.commit,
                    events=tuple(
                        e for e in message.commit.events if e.schema in schemas
                    ),
                ),
                subscription_ids=tuple(subscription_ids),
                query_events=tuple(
                    e
                    for e in message.query_events
                    if e.subscription_id in covered
                ),
            )

    def release(
        self, client_id: str, subscription_id: str
    ) -> HostSubscription | None:
        with self._store.synchronized():
            subscription = self.get(client_id, subscription_id)

            if subscription is not None:
                self._remove(subscription)

            return subscription

    def remove_client(self, client_id: str) -> tuple[HostSubscription, ...]:
        with self._store.synchronized():
            subscriptions = self.for_client(client_id)

            for subscription in subscriptions:
                self._remove(subscription)

            return subscriptions

    def clear(self) -> None:
        with self._store.synchronized():
            self._by_id.clear()
            self._by_client.clear()
            self._by_schema.clear()
            self._queries.clear()

    def _acquire(
        self, client_id: str, source: str | HostQuery, revision: int
    ) -> tuple[HostSubscription, bool]:
        key = source if isinstance(source, str) else source.source
        existing = self._by_client.get(client_id, {}).get(key)

        if existing is not None:
            existing.revision.hydrate(revision)
            return existing, False

        subscription = HostSubscription(
            subscription_id=uuid4().hex,
            client_id=client_id,
            source=source,
            revision=SubscriptionRevision(revision),
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
        del self._by_id[subscription_id]
        sources = self._by_client[client_id]
        del sources[subscription.key]

        if not sources:
            del self._by_client[client_id]

        for schema in subscription.dependencies:
            subscribers = self._by_schema[schema]
            del subscribers[subscription_id]

            if not subscribers:
                del self._by_schema[schema]

        if isinstance(subscription.source, HostQuery):
            self._queries.release(subscription.source)

    def __len__(self) -> int:
        with self._store.synchronized():
            return len(self._by_id)
