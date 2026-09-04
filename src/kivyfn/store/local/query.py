from __future__ import annotations

import inspect
import weakref

from collections.abc import Callable, Iterable, Mapping
from threading import RLock
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

from kivyfn.logging import get_logger
from kivyfn.store.query import Query, QueryChangeSet
from kivyfn.store.query.event import (
    QueryResult,
    copy_query_result,
    diff_query_results,
)
from kivyfn.store.utils.tools.uuid import generate_uuid


if TYPE_CHECKING:
    from kivyfn.store.core.engine import Engine
    from kivyfn.store.events.event import StoreEvent
    from kivyfn.store.query.plan import QueryPlan


logger = get_logger(__name__)

QueryCallback = Callable[[QueryChangeSet], None]


class _CallbackReference:
    __slots__ = (
        "_strong",
        "_weak",
    )

    def __init__(self, callback: QueryCallback) -> None:
        self._strong: QueryCallback | None = None
        self._weak: weakref.WeakMethod[Any] | None = None

        if inspect.ismethod(callback) and callback.__self__ is not None:
            self._weak = weakref.WeakMethod(callback)
        else:
            self._strong = callback

    def get(self) -> QueryCallback | None:
        if self._weak is not None:
            return cast(QueryCallback | None, self._weak())

        return self._strong


class LocalQuerySubscription:
    __slots__ = (
        "__weakref__",
        "_callback",
        "_data",
        "_disposed",
        "_manager_ref",
        "_plan",
        "_sequence",
        "_token",
    )

    def __init__(
        self,
        manager: LocalQuerySubscriptionManager,
        plan: QueryPlan,
        callback: QueryCallback,
        data: QueryResult,
    ) -> None:
        self._manager_ref = weakref.ref(manager)
        self._token = generate_uuid()
        self._plan = plan
        self._callback = _CallbackReference(callback)
        self._data = copy_query_result(data)
        self._sequence = 0
        self._disposed = False

    def dispose(self) -> None:
        if self._disposed:
            return

        manager = self._manager_ref()

        if manager is None:
            self._mark_disposed()
            return

        manager.unsubscribe(self._token)

    def refresh(self, events: tuple[StoreEvent, ...]) -> bool:
        if self._disposed:
            return False

        callback = self._callback.get()

        if callback is None:
            return False

        addresses = {event.address for event in events}

        if addresses.isdisjoint(self._plan.dependencies):
            return True

        manager = self._manager_ref()

        if manager is None:
            return False

        current = manager.engine.execute_query(self._plan)
        query_events = diff_query_results(self._data, current)
        self._data = copy_query_result(current)
        self._sequence += 1

        if not query_events:
            return True

        change = QueryChangeSet(
            sequence=self._sequence,
            events=query_events,
        )

        try:
            result = callback(change)

            if inspect.isawaitable(result):
                close = getattr(result, "close", None)

                if callable(close):
                    close()

                raise TypeError(
                    "Local query callbacks must be synchronous"
                )
        except Exception:
            logger.exception(
                "Local query subscription callback failed: token=%s",
                self._token,
            )

        return True

    def _mark_disposed(self) -> None:
        self._disposed = True

    @property
    def token(self) -> str:
        return self._token

    @property
    def query(self) -> Query:
        return self._plan.query

    @property
    def data(self) -> Mapping[str, Mapping[str, Any]]:
        return MappingProxyType(self._data)

    @property
    def sequence(self) -> int:
        return self._sequence

    @property
    def callback_alive(self) -> bool:
        return self._callback.get() is not None

    @property
    def disposed(self) -> bool:
        return self._disposed


class LocalQuerySubscriptionManager:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._lock = RLock()
        self._subscriptions: dict[
            str,
            LocalQuerySubscription,
        ] = {}

    def subscribe(
        self,
        query: Query,
        callback: QueryCallback,
    ) -> LocalQuerySubscription:
        with self._lock:
            plan = self._engine.compile_query(query)
            data = self._engine.execute_query(plan)
            subscription = LocalQuerySubscription(
                manager=self,
                plan=plan,
                callback=callback,
                data=data,
            )
            self._subscriptions[subscription.token] = subscription
            return subscription

    def unsubscribe(self, token: str) -> bool:
        with self._lock:
            subscription = self._subscriptions.pop(token, None)

        if subscription is None:
            return False

        subscription._mark_disposed()
        return True

    def dispatch_many(self, events: Iterable[StoreEvent]) -> None:
        event_values = tuple(events)

        if not event_values:
            return

        with self._lock:
            subscriptions = tuple(self._subscriptions.values())

        dead: list[str] = []

        for subscription in subscriptions:
            if not subscription.refresh(event_values):
                dead.append(subscription.token)

        for token in dead:
            self.unsubscribe(token)

    def clear(self) -> None:
        with self._lock:
            subscriptions = tuple(self._subscriptions.values())
            self._subscriptions.clear()

        for subscription in subscriptions:
            subscription._mark_disposed()

    @property
    def engine(self) -> Engine:
        return self._engine
