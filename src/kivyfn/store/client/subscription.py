# src/kivyfn/store/client/subscription.py
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any, TypeAlias

from kivyfn.store.query import Query
from kivyfn.store.schema import ManySchema, SingleSchema


SubscriptionSource: TypeAlias = (
    type[SingleSchema] | type[ManySchema] | Query[Any]
)


class ClientSubscription:
    """One desired subscription and its last confirmed schema revisions.

    StoreClient owns access on its event loop and rejects callbacks from old
    connection generations before calling these methods. Consumers own the
    actual confirmed data; this record never stores optimistic values.

    Binding a response does not initialize data. Validate a snapshot before
    applying it, then hydrate() only after every consumer has a complete
    confirmed view. Check events before applying them, and advance() only
    after all consumers succeed. Invalidate on application failure or loss
    of complete cached data, including data discarded by a weak registry.
    """

    __slots__ = (
        "_source",
        "_subscription_id",
        "_store_instance_id",
        "_schema_revisions",
        "_cache_valid",
        "_initialized",
        "_consumers",
    )

    def __init__(self, source: SubscriptionSource) -> None:
        self._source = _normalize(source)
        self._subscription_id: str | None = None
        self._store_instance_id: str | None = None
        self._schema_revisions: dict[str, int] = {}
        self._cache_valid = False
        self._initialized = False
        self._consumers: dict[int, object] = {}

    @property
    def source(self) -> SubscriptionSource:
        return self._source

    @property
    def subscription_id(self) -> str | None:
        return self._subscription_id

    @property
    def store_instance_id(self) -> str | None:
        return self._store_instance_id

    @property
    def schema_revisions(self) -> dict[str, int]:
        """Return a copy of the last successfully applied schema revisions."""
        return dict(self._schema_revisions)

    @property
    def active(self) -> bool:
        return self._subscription_id is not None

    @property
    def ready(self) -> bool:
        return self.active and self._initialized and self._cache_valid

    @property
    def cache_valid(self) -> bool:
        return self._cache_valid

    @property
    def consumer_count(self) -> int:
        return len(self._consumers)

    @property
    def consumers(self) -> tuple[object, ...]:
        return tuple(self._consumers.values())

    def cache_hint(self) -> tuple[str | None, dict[str, int] | None]:
        """Return both cache hints, or neither when a snapshot is required."""
        if not self._cache_valid:
            return None, None
        return self._store_instance_id, dict(self._schema_revisions)

    def attach(self, consumer: object) -> bool:
        """Attach by identity; a new consumer needs complete initialization."""
        key = id(consumer)
        if self._consumers.get(key) is consumer:
            return False

        self._consumers[key] = consumer
        self.invalidate()
        return True

    def detach(self, consumer: object) -> bool:
        key = id(consumer)
        if self._consumers.get(key) is not consumer:
            return False

        del self._consumers[key]
        if not self._consumers:
            self.invalidate()
        return True

    def has_consumer(self, consumer: object) -> bool:
        return self._consumers.get(id(consumer)) is consumer

    def bind(self, subscription_id: str, store_instance_id: str) -> None:
        """Bind the current connection's response without certifying data."""
        _validate_id(subscription_id, "Subscription ID")
        _validate_id(store_instance_id, "Store instance ID")

        if self.active and self._subscription_id != subscription_id:
            raise RuntimeError("Cannot change an active subscription's ID")
        if self.active and self._store_instance_id != store_instance_id:
            raise RuntimeError("Cannot change an active subscription's Store")

        if self._store_instance_id != store_instance_id:
            self.invalidate()
            self._schema_revisions.clear()

        self._subscription_id = subscription_id
        self._store_instance_id = store_instance_id
        self._initialized = False

    def check_snapshot(
        self, revisions: Mapping[str, int], *, unchanged: bool = False
    ) -> dict[str, int]:
        """Validate a complete snapshot before any consumer applies its data.

        StoreClient also validates query shape and dependency coverage from
        the response. Unchanged responses require an exact retained cache.
        Fresh snapshots may jump forward within the same Store lifetime.
        """
        if not self.active or not self._consumers:
            raise RuntimeError("Snapshot needs a bound source and consumers")

        values = _copy_revisions(revisions)
        source = self._source
        if isinstance(source, Query):
            root = source.schema_name
        else:
            root = source.__name__
        if root not in values:
            raise ValueError("Snapshot must include the root schema")
        if not isinstance(source, Query) and set(values) != {root}:
            raise ValueError("Schema snapshot cannot include other schemas")

        previous = self._schema_revisions
        if previous and values.keys() != previous.keys():
            raise ValueError("Dependencies changed within one Store instance")
        if any(values[schema] < value for schema, value in previous.items()):
            raise ValueError("Snapshot revisions cannot move backwards")
        if unchanged and (not self._cache_valid or values != previous):
            raise ValueError("Cache reuse requires complete matching data")
        return values

    def hydrate(
        self, revisions: Mapping[str, int], *, unchanged: bool = False
    ) -> None:
        """Confirm successful snapshot installation or verified cache reuse."""
        values = self.check_snapshot(revisions, unchanged=unchanged)
        self._schema_revisions = values
        self._cache_valid = True
        self._initialized = True

    def unbind(self) -> str | None:
        """Drop the host ID, keeping consumers and valid confirmed data."""
        subscription_id = self._subscription_id
        self._subscription_id = None
        self._initialized = False
        return subscription_id

    def invalidate(self) -> None:
        """Require a snapshot after data loss or an unreliable stream."""
        self._cache_valid = False
        self._initialized = False

    def check_event(
        self,
        subscription_id: str,
        store_instance_id: str,
        revisions: Mapping[str, int],
    ) -> bool:
        """Validate before applying an event; return False for stale coverage.

        Only changed dependencies of this subscription advance. Unrelated
        schema revisions do not certify any of its data. Gaps or partly
        covered commits invalidate the cache without moving its counters.
        Events arriving during initialization must be buffered by the owner.
        """
        if (
            subscription_id != self._subscription_id
            or store_instance_id != self._store_instance_id
            or not self.active
        ):
            return False
        if not self.ready:
            raise RuntimeError("Subscription needs an initialized snapshot")

        try:
            values = _copy_revisions(revisions)
            relevant = {
                schema: value
                for schema, value in values.items()
                if schema in self._schema_revisions
            }
            if all(
                value <= self._schema_revisions[schema]
                for schema, value in relevant.items()
            ):
                return False

            for schema, value in relevant.items():
                expected = self._schema_revisions[schema] + 1
                if value != expected:
                    raise ValueError(
                        f"Subscription revision for {schema!r}: "
                        f"expected {expected}, got {value}"
                    )
        except (TypeError, ValueError):
            self.invalidate()
            raise
        return True

    def advance(
        self,
        subscription_id: str,
        store_instance_id: str,
        revisions: Mapping[str, int],
    ) -> bool:
        """Confirm an event after every consumer applied it successfully."""
        if not self.check_event(subscription_id, store_instance_id, revisions):
            return False

        self._schema_revisions.update(
            (schema, value)
            for schema, value in revisions.items()
            if schema in self._schema_revisions
        )
        return True


class ClientSubscriptions:
    """Deduplicate desired sources and route only their current host IDs."""

    def __init__(self) -> None:
        self._by_source: dict[SubscriptionSource, ClientSubscription] = {}
        self._by_id: dict[str, ClientSubscription] = {}

    @property
    def desired(self) -> tuple[ClientSubscription, ...]:
        """Subscriptions with consumers to recreate after reconnecting."""
        return tuple(s for s in self._by_source.values() if s.consumer_count)

    def acquire(
        self, source: SubscriptionSource, consumer: object
    ) -> tuple[ClientSubscription, bool]:
        """Attach a consumer and report whether a local record was created.

        The caller initializes a new consumer even for an existing source.
        Existing records may also need resubscription after a disconnect.
        """
        source = _normalize(source)
        subscription = self._by_source.get(source)
        created = subscription is None
        if subscription is None:
            subscription = ClientSubscription(source)
            self._by_source[source] = subscription

        subscription.attach(consumer)
        return subscription, created

    def release(
        self, source: SubscriptionSource, consumer: object
    ) -> tuple[ClientSubscription, bool]:
        """Detach a consumer; True means the host subscription is unused."""
        subscription = self._by_source.get(_normalize(source))
        if subscription is None:
            raise KeyError("Subscription source is not registered")
        if not subscription.detach(consumer):
            raise KeyError("Subscription consumer is not registered")
        return subscription, subscription.consumer_count == 0

    def bind(
        self,
        subscription: ClientSubscription,
        subscription_id: str,
        store_instance_id: str,
    ) -> None:
        if self._by_source.get(subscription.source) is not subscription:
            raise ValueError("Subscription is not registered with this client")
        if not subscription.consumer_count:
            raise RuntimeError("Cannot bind a subscription without consumers")
        existing = self._by_id.get(subscription_id)
        if existing is not None and existing is not subscription:
            raise ValueError("Subscription ID is already bound")

        subscription.bind(subscription_id, store_instance_id)
        self._by_id[subscription_id] = subscription

    def unbind(self, subscription: ClientSubscription) -> None:
        if self._by_source.get(subscription.source) is not subscription:
            return
        subscription_id = subscription.unbind()
        if (
            subscription_id is not None
            and self._by_id.get(subscription_id) is subscription
        ):
            self._by_id.pop(subscription_id, None)

    def remove(self, subscription: ClientSubscription) -> None:
        if self._by_source.get(subscription.source) is not subscription:
            return
        if subscription.consumer_count:
            raise RuntimeError("Cannot remove a subscription with consumers")
        self.unbind(subscription)
        subscription.invalidate()
        del self._by_source[subscription.source]

    def get(self, source: SubscriptionSource) -> ClientSubscription | None:
        return self._by_source.get(_normalize(source))

    def get_by_id(self, subscription_id: str) -> ClientSubscription | None:
        return self._by_id.get(subscription_id)

    def unbind_all(self) -> None:
        """Drop host identities while preserving desired subscriptions."""
        for subscription in self._by_source.values():
            subscription.unbind()
        self._by_id.clear()

    def __len__(self) -> int:
        return len(self._by_source)


def _normalize(source: SubscriptionSource) -> SubscriptionSource:
    if isinstance(source, Query):
        schema = source.schema_name
        if schema is None:
            raise ValueError("Live queries require a schema name or class")
        return replace(source, source=schema)
    if not isinstance(source, type) or not issubclass(
        source, (SingleSchema, ManySchema)
    ):
        raise TypeError("Subscriptions require a schema class or a live Query")
    return source


def _copy_revisions(revisions: Mapping[str, int]) -> dict[str, int]:
    values = dict(revisions)
    for schema, value in values.items():
        _validate_id(schema, "Schema name")
        if type(value) is not int or value < 0:
            raise ValueError("Schema revisions must be non-negative integers")
    return values


def _validate_id(value: str, name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
