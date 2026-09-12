# src/kivyfn/store/host/subscription.py
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, TypeAlias
from uuid import uuid4

from kivyfn.store.error import InvalidOperationError, StoreStateError
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
    """One owned subscription with its source and schema delivery progress."""

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

    Resolve schema kinds and query dependencies before checking cache hints.
    Store's lock covers snapshots, revision comparisons, and registration.
    None snapshots certify a matching cache; empty snapshots replace data.

    Capture runs synchronously during commit publication. The runtime queues
    messages for ordered delivery, including to the requesting subscriber.
    Clients must coordinate snapshot installation with buffered live events.
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

            revision, snapshot = self._store.snapshot_if_changed(
                request.schema,
                revision=request.revision,
                server_instance_id=request.store_instance_id,
            )
            subscription, _ = self._acquire(
                client_id, request.schema, {request.schema: revision}
            )

            if single:
                return SingleSubscriptionResponse(
                    request_id=request.request_id,
                    subscription_id=subscription.subscription_id,
                    store_instance_id=self._store.instance_id,
                    revision=revision,
                    values=(
                        None if snapshot is None else tuple(snapshot.items())
                    ),
                )

            rows = None
            if snapshot is not None:
                rows = tuple(QueryRow(id, row) for id, row in snapshot.items())

            return CollectionSubscriptionResponse(
                request_id=request.request_id,
                subscription_id=subscription.subscription_id,
                store_instance_id=self._store.instance_id,
                revision=revision,
                rows=rows,
            )

    def subscribe_query(
        self, client_id: str, request: QuerySubscriptionRequest
    ) -> QuerySubscriptionResponse:
        with self._store.synchronized():
            query = self._queries.acquire(request.query)
            created = False
            try:
                revisions = query.schema_revisions
                cached = request.schema_revisions
                unchanged = (
                    request.store_instance_id == self._store.instance_id
                    and cached is not None
                    and all(type(value) is int for value in cached.values())
                    and cached == revisions
                )
                subscription, created = self._acquire(
                    client_id, query, revisions
                )

                return QuerySubscriptionResponse(
                    request_id=request.request_id,
                    subscription_id=subscription.subscription_id,
                    store_instance_id=self._store.instance_id,
                    schema_revisions=revisions,
                    shape=query.shape,
                    rows=None if unchanged else query.rows,
                )
            finally:
                if not created:
                    self._queries.release(query)

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

            for schema in commit.schema_revisions:
                selected.update(self._by_schema.get(schema, {}))

            return tuple(selected.values())

    def capture(
        self, commit: StoreCommit
    ) -> tuple[tuple[str, EventMessage], ...]:
        """Capture a complete Store commit before asynchronous delivery."""
        with self._store.synchronized():
            self._require_instance(commit)
            changed_schemas = {event.schema for event in commit.events}
            if changed_schemas != set(commit.schema_revisions):
                raise StoreStateError(
                    "Store commit revisions must match its changes"
                )

            selected: dict[str, list[HostSubscription]] = {}

            for subscription in self.affected(commit):
                if not subscription.revision.covers(commit.schema_revisions):
                    selected.setdefault(subscription.client_id, []).append(
                        subscription
                    )

            messages: list[tuple[str, EventMessage]] = []

            for client_id, subscriptions in selected.items():
                query_events: list[QueryEvent] = []

                for subscription in subscriptions:
                    source = subscription.source

                    if isinstance(source, HostQuery):
                        if not source.capture(commit.schema_revisions):
                            raise StoreStateError(
                                "Query changes do not cover this subscription"
                            )
                        event = source.event(subscription.subscription_id)

                        if event is not None:
                            query_events.append(event)

                message = _message(commit, subscriptions, query_events)
                messages.append((client_id, message))

            return tuple(messages)

    def prepare(
        self, client_id: str, message: EventMessage
    ) -> EventMessage | None:
        """Filter covered changes and validate all progress before advancing.

        This is delivery progress, not a client acknowledgement. The runtime
        must invalidate the connection if validation or transport delivery
        fails. A later snapshot or release can discard already queued data.
        """
        with self._store.synchronized():
            self._require_instance(message.commit)
            revisions = message.commit.schema_revisions
            subscriptions: list[HostSubscription] = []
            changed_schemas = {event.schema for event in message.commit.events}

            for subscription_id in dict.fromkeys(message.subscription_ids):
                subscription = self.get(client_id, subscription_id)

                if subscription is None:
                    continue

                candidate = SubscriptionRevision(subscription.revision.current)
                if not candidate.accept(revisions):
                    continue

                source = subscription.source
                if isinstance(source, str) and source not in changed_schemas:
                    raise StoreStateError(
                        "Schema update is missing its canonical events"
                    )
                subscriptions.append(subscription)

            if not subscriptions:
                return None

            prepared = _message(
                message.commit, subscriptions, message.query_events
            )
            for subscription in subscriptions:
                subscription.revision.accept(revisions)
            return prepared

    def _require_instance(self, commit: StoreCommit) -> None:
        if commit.server_instance_id != self._store.instance_id:
            raise StoreStateError(
                "Subscription commit belongs to another Store"
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
        self,
        client_id: str,
        source: str | HostQuery,
        revisions: Mapping[str, int],
    ) -> tuple[HostSubscription, bool]:
        key = source if isinstance(source, str) else source.source
        existing = self._by_client.get(client_id, {}).get(key)

        if existing is not None:
            existing.revision.hydrate(revisions)
            return existing, False

        subscription = HostSubscription(
            subscription_id=uuid4().hex,
            client_id=client_id,
            source=source,
            revision=SubscriptionRevision(revisions),
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


def _message(
    commit: StoreCommit,
    subscriptions: Sequence[HostSubscription],
    query_events: Sequence[QueryEvent],
) -> EventMessage:
    """Limit events and revision metadata to covered subscriptions."""
    schemas = {
        subscription.source
        for subscription in subscriptions
        if isinstance(subscription.source, str)
    }
    dependencies = {
        schema
        for subscription in subscriptions
        for schema in subscription.dependencies
    }
    subscription_ids = tuple(s.subscription_id for s in subscriptions)
    covered = set(subscription_ids)
    return EventMessage(
        commit=replace(
            commit,
            events=tuple(e for e in commit.events if e.schema in schemas),
            schema_revisions={
                schema: revision
                for schema, revision in commit.schema_revisions.items()
                if schema in dependencies
            },
        ),
        subscription_ids=subscription_ids,
        query_events=tuple(
            e for e in query_events if e.subscription_id in covered
        ),
    )
