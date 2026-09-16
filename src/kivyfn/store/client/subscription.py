# src/kivyfn/store/client/subscription.py
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol, TypeAlias

from kivyfn.store.error import StoreFailure, StoreProtocolError
from kivyfn.store.protocol.message.event import (
    EventAction,
    SignalEventAction,
    SignalRowEventAction,
    SubscriptionEvent,
)
from kivyfn.store.protocol.message.requests import (
    CollectionSubscriptionRequest,
    QuerySubscriptionRequest,
    SingleSubscriptionRequest,
    SubscriptionRequest,
)
from kivyfn.store.protocol.message.responses import (
    CollectionSubscriptionResponse,
    QuerySubscriptionResponse,
    SingleSubscriptionResponse,
    SubscriptionResponse,
)
from kivyfn.store.query import Query
from kivyfn.store.schema import ManySchema, SingleSchema

SubscriptionSource: TypeAlias = (
    type[SingleSchema] | type[ManySchema] | Query[Any]
)


@dataclass(frozen=True, slots=True)
class SubscriptionUpdate:
    """Actions for a source, resolved and validated by the client."""

    source: SubscriptionSource
    actions: tuple[EventAction, ...]


@dataclass(frozen=True, slots=True)
class SubscriptionPublication:
    """Retained notifications and ordered occurrences for one staged view."""

    publish: Callable[[], None]
    signals: tuple[tuple[int | None, Callable[[], None]], ...] = ()

    def __call__(self) -> None:
        publish_subscriptions((self,))


def publish_subscriptions(
    publications: Iterable[SubscriptionPublication],
) -> None:
    """Publish retained changes, then signals in original commit order."""
    signals: list[tuple[int | None, Callable[[], None]]] = []
    for publication in publications:
        publication.publish()
        signals.extend(publication.signals)
    # An occurrence ID is shared across subscription fan-out. Stable sorting
    # retains row/collection dispatch order within the same occurrence.
    signals.sort(key=lambda item: (item[0] is None, item[0] or 0))
    for _, emit in signals:
        emit()


class SubscriptionManager(Protocol):
    """The reactive consumer of client-validated subscription delivery."""

    def on_subscription_snapshot(
        self, source: SubscriptionSource, response: SubscriptionResponse
    ) -> None: ...

    def prepare_subscription_updates(
        self,
        updates: tuple[SubscriptionUpdate, ...],
        *,
        transaction_id: str | None = None,
    ) -> SubscriptionPublication: ...

    def on_subscription_status(
        self, source: SubscriptionSource, synced: bool
    ) -> None: ...


@dataclass(slots=True)
class ClientSubscription:
    """One desired subscription retained across connections."""

    source: SubscriptionSource
    manager: SubscriptionManager
    subscription_id: str | None = None
    revisions: dict[str, int] | None = None
    failure: StoreFailure | None = None

    @property
    def active(self) -> bool:
        return self.subscription_id is not None

    def request(self, store_instance_id: str | None) -> SubscriptionRequest:
        cached = self.revisions is not None and store_instance_id is not None

        instance_id = store_instance_id if cached else None

        source = self.source

        if isinstance(source, Query):
            return QuerySubscriptionRequest(
                query=source,
                store_instance_id=instance_id,
                revisions=(
                    dict(self.revisions)
                    if cached and self.revisions is not None
                    else None
                ),
            )

        schema = source.__name__
        revision = (
            self.revisions[schema]
            if cached and self.revisions is not None
            else None
        )

        if issubclass(source, SingleSchema):
            return SingleSubscriptionRequest(
                schema=schema, store_instance_id=instance_id, revision=revision
            )

        return CollectionSubscriptionRequest(
            schema=schema, store_instance_id=instance_id, revision=revision
        )

    def response_revisions(
        self, response: SubscriptionResponse, *, store_instance_id: str | None
    ) -> dict[str, int]:
        revisions = self._response_revisions(response)

        if _has_snapshot(response):
            return revisions

        if self.revisions is None:
            raise StoreProtocolError(
                "Subscription without cached revisions received no snapshot"
            )

        if store_instance_id != response.store_instance_id:
            raise StoreProtocolError("New Store instance returned no snapshot")

        if self.revisions != revisions:
            raise StoreProtocolError(
                "Subscription revisions changed without a snapshot"
            )

        return revisions

    def next_revisions(self, event: SubscriptionEvent) -> dict[str, int]:
        if event.subscription_id != self.subscription_id:
            raise StoreProtocolError("Subscription event ID does not match")

        current = self.revisions

        if current is None:
            raise StoreProtocolError(
                "Subscription received an event before synchronization"
            )

        if not set(event.revisions).issubset(current):
            raise StoreProtocolError(
                "Subscription event contains an unknown dependency"
            )

        if not event.revisions and any(
            not isinstance(action, (SignalEventAction, SignalRowEventAction))
            for action in event.actions
        ):
            raise StoreProtocolError("Retained changes require a revision")

        revisions = dict(current)

        for schema, revision in event.revisions.items():
            previous = current[schema]

            if revision != previous + 1:
                raise StoreProtocolError(
                    f"Subscription revision gap for {schema!r}: "
                    f"expected {previous + 1}, got {revision}"
                )

            revisions[schema] = revision

        return revisions

    def advance(self, revisions: dict[str, int]) -> None:
        self.revisions = dict(revisions)

    def _response_revisions(
        self, response: SubscriptionResponse
    ) -> dict[str, int]:
        source = self.source

        if isinstance(source, Query):
            if not isinstance(response, QuerySubscriptionResponse):
                raise StoreProtocolError("Invalid Query subscription response")

            return dict(response.revisions)

        schema = source.__name__

        if issubclass(source, SingleSchema):
            if not isinstance(response, SingleSubscriptionResponse):
                raise StoreProtocolError(
                    "Invalid Single subscription response"
                )

            return {schema: response.revision}

        if not isinstance(response, CollectionSubscriptionResponse):
            raise StoreProtocolError(
                "Invalid Collection subscription response"
            )

        return {schema: response.revision}


class ClientSubscriptions:
    """Own desired subscriptions and current connection bindings."""

    def __init__(self) -> None:
        self._by_source: dict[SubscriptionSource, ClientSubscription] = {}

        self._by_id: dict[str, ClientSubscription] = {}

    @property
    def synced(self) -> bool:
        return all(
            subscription.active for subscription in self._by_source.values()
        )

    def get_or_create(
        self, source: SubscriptionSource, manager: SubscriptionManager
    ) -> ClientSubscription:
        subscription = self._by_source.get(source)

        if subscription is not None:
            if subscription.manager is not manager:
                raise RuntimeError(
                    "Subscription belongs to another StateManager"
                )

            return subscription

        subscription = ClientSubscription(source=source, manager=manager)

        self._by_source[source] = subscription

        return subscription

    def get(self, source: SubscriptionSource) -> ClientSubscription | None:
        return self._by_source.get(source)

    def get_by_id(self, subscription_id: str) -> ClientSubscription | None:
        return self._by_id.get(subscription_id)

    def bind(
        self,
        subscription: ClientSubscription,
        response: SubscriptionResponse,
        revisions: dict[str, int],
    ) -> None:
        current = self._by_source.get(subscription.source)

        if current is not subscription:
            raise RuntimeError("Subscription is no longer registered")

        subscription_id = response.subscription_id

        existing = self._by_id.get(subscription_id)

        if existing is not None and existing is not subscription:
            raise StoreProtocolError("Subscription ID is already bound")

        old_id = subscription.subscription_id

        if old_id is not None and old_id != subscription_id:
            self._by_id.pop(old_id, None)

        subscription.subscription_id = subscription_id
        subscription.revisions = dict(revisions)
        subscription.failure = None

        self._by_id[subscription_id] = subscription

    def remove(self, source: SubscriptionSource) -> ClientSubscription | None:
        subscription = self._by_source.pop(source, None)

        if subscription is None:
            return None

        self._unbind(subscription)

        return subscription

    def disconnect(self) -> None:
        for subscription in tuple(self._by_source.values()):
            self._unbind(subscription)
            subscription.failure = None

        self._by_id.clear()

    def inactive(self) -> tuple[ClientSubscription, ...]:
        return tuple(
            subscription
            for subscription in self._by_source.values()
            if not subscription.active
        )

    def desired(self) -> tuple[ClientSubscription, ...]:
        return tuple(self._by_source.values())

    def __len__(self) -> int:
        return len(self._by_source)

    def _unbind(self, subscription: ClientSubscription) -> None:
        subscription_id = subscription.subscription_id

        if subscription_id is None:
            return

        self._by_id.pop(subscription_id, None)

        subscription.subscription_id = None


def _has_snapshot(response: SubscriptionResponse) -> bool:
    if isinstance(response, SingleSubscriptionResponse):
        return response.values is not None

    return response.rows is not None
