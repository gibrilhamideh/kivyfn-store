# src/kivyfn/store/client/subscription.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TypeAlias

from kivyfn.store.query import Query
from kivyfn.store.schema import ManySchema, SingleSchema


SubscriptionSource: TypeAlias = (
    type[SingleSchema] | type[ManySchema] | Query[Any]
)


@dataclass(slots=True)
class ClientSubscription:
    """One deduplicated subscription owned by a StoreClient."""

    source: SubscriptionSource
    subscription_id: str | None = None
    store_instance_id: str | None = None
    revision: int | None = None

    _consumers: dict[int, object] = field(
        default_factory=dict, init=False, repr=False
    )

    @property
    def active(self) -> bool:
        return self.subscription_id is not None

    @property
    def consumer_count(self) -> int:
        return len(self._consumers)

    @property
    def consumers(self) -> tuple[object, ...]:
        return tuple(self._consumers.values())

    def attach(self, consumer: object) -> bool:
        """Attach a consumer by identity.

        Returns True when the consumer was newly attached.
        """

        key = id(consumer)

        existing = self._consumers.get(key)

        if existing is consumer:
            return False

        self._consumers[key] = consumer
        return True

    def detach(self, consumer: object) -> bool:
        """Detach a consumer by identity.

        Returns True when the consumer was attached.
        """

        key = id(consumer)

        existing = self._consumers.get(key)

        if existing is not consumer:
            return False

        del self._consumers[key]
        return True

    def has_consumer(self, consumer: object) -> bool:
        key = id(consumer)

        return self._consumers.get(key) is consumer

    def bind(
        self, subscription_id: str, store_instance_id: str, revision: int
    ) -> None:
        """Bind this subscription to its current host identity."""

        _validate_id(subscription_id, "Subscription ID")
        _validate_id(store_instance_id, "Store instance ID")
        _validate_revision(revision)

        if (
            self.subscription_id is not None
            and self.subscription_id != subscription_id
        ):
            raise RuntimeError(
                "Active subscription cannot change subscription ID"
            )

        if (
            self.store_instance_id is not None
            and self.store_instance_id != store_instance_id
            and self.subscription_id is not None
        ):
            raise RuntimeError(
                "Active subscription cannot change Store instance"
            )

        self.subscription_id = subscription_id
        self.store_instance_id = store_instance_id
        self.revision = revision

    def unbind(self) -> str | None:
        """Drop the host subscription while preserving sync history."""

        subscription_id = self.subscription_id
        self.subscription_id = None

        return subscription_id

    def advance(self, revision: int) -> None:
        """Advance this subscription through a Store revision."""

        _validate_revision(revision)

        current = self.revision

        if current is not None and revision < current:
            raise ValueError("Subscription revision cannot move backwards")

        self.revision = revision


class ClientSubscriptions:
    """Deduplicates StoreClient subscriptions by semantic source."""

    def __init__(self) -> None:
        self._by_source: dict[SubscriptionSource, ClientSubscription] = {}
        self._by_id: dict[str, ClientSubscription] = {}

    def acquire(
        self, source: SubscriptionSource, consumer: object
    ) -> tuple[ClientSubscription, bool]:
        """Acquire a source subscription for one consumer.

        The boolean is True when the source needs a host subscription.
        """

        subscription = self._by_source.get(source)

        created = subscription is None

        if subscription is None:
            subscription = ClientSubscription(source=source)
            self._by_source[source] = subscription

        subscription.attach(consumer)

        return subscription, created

    def release(
        self, source: SubscriptionSource, consumer: object
    ) -> tuple[ClientSubscription, bool]:
        """Release one consumer.

        The boolean is True when the host subscription can be removed.
        """

        subscription = self._by_source.get(source)

        if subscription is None:
            raise KeyError("Subscription source is not registered")

        if not subscription.detach(consumer):
            raise KeyError("Subscription consumer is not registered")

        return (subscription, subscription.consumer_count == 0)

    def bind(
        self,
        subscription: ClientSubscription,
        subscription_id: str,
        store_instance_id: str,
        revision: int,
    ) -> None:
        existing = self._by_id.get(subscription_id)

        if existing is not None and existing is not subscription:
            raise ValueError("Subscription ID is already bound")

        old_id = subscription.subscription_id

        subscription.bind(subscription_id, store_instance_id, revision)

        if old_id is not None and old_id != subscription_id:
            self._by_id.pop(old_id, None)

        self._by_id[subscription_id] = subscription

    def unbind(self, subscription: ClientSubscription) -> None:
        subscription_id = subscription.unbind()

        if subscription_id is not None:
            self._by_id.pop(subscription_id, None)

    def remove(self, subscription: ClientSubscription) -> None:
        if subscription.consumer_count:
            raise RuntimeError("Cannot remove a subscription with consumers")

        self.unbind(subscription)

        current = self._by_source.get(subscription.source)

        if current is subscription:
            del self._by_source[subscription.source]

    def get(self, source: SubscriptionSource) -> ClientSubscription | None:
        return self._by_source.get(source)

    def get_by_id(self, subscription_id: str) -> ClientSubscription | None:
        return self._by_id.get(subscription_id)

    def unbind_all(self) -> None:
        """Drop host IDs while retaining consumers and sync cursors."""

        for subscription in self._by_source.values():
            subscription.unbind()

        self._by_id.clear()

    def __len__(self) -> int:
        return len(self._by_source)


def _validate_revision(revision: int) -> None:
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 0
    ):
        raise ValueError("Store revision must be a non-negative integer")


def _validate_id(value: str, name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
