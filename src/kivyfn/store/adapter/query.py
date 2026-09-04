from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from kivy.event import EventDispatcher

from kivyfn.logging import get_logger
from kivyfn.store.error import StoreStateError
from kivyfn.store.query import (
    Query,
    QueryChangeSet,
    QueryInsertEvent,
    QueryRemoveEvent,
    QueryResult,
    QueryUpdateEvent,
)
from kivyfn.store.query.event import (
    apply_query_change,
    copy_query_result,
)
from kivyfn.store.remote.query import RemoteQuerySubscription
from kivyfn.store.remote.store import RemoteStore


logger = get_logger(__name__)


class QueryAdapter(EventDispatcher):
    """Keep one remote materialized query view synchronized."""

    __events__ = (
        "on_loaded",
        "on_insert",
        "on_update",
        "on_remove",
        "on_sync_lost",
        "on_resynchronized",
    )

    def __init__(
        self,
        store: RemoteStore,
        query: Query,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._store = store
        self._query = query
        self._subscription: RemoteQuerySubscription | None = None
        self._data: QueryResult | None = None
        self._loading = False
        self._synchronized = False

    async def load(self) -> None:
        if self.loaded:
            raise StoreStateError(
                "Query adapter is already loaded"
            )

        if self._loading:
            raise StoreStateError(
                "Query adapter is already loading"
            )

        self._loading = True

        try:
            response = await self.store.client.subscribe_query(
                self.query,
                self._on_query_change,
                sync_callback=self._on_sync_change,
            )

            if response.ok is False:
                raise StoreStateError(
                    "Could not subscribe query adapter: "
                    f"{response.error}"
                )

            self._subscription = response.data
            self._data = copy_query_result(response.data.data)
            self._synchronized = response.data.synchronized
        except BaseException:
            self._subscription = None
            self._data = None
            self._synchronized = False
            raise
        finally:
            self._loading = False

        self.dispatch("on_loaded")

    async def resync(self, reason: str = "manual") -> None:
        subscription = self._require_subscription()
        await subscription.resync(reason)

    async def dispose(self) -> None:
        subscription = self._subscription

        if subscription is None:
            return

        self._subscription = None
        self._data = None
        self._synchronized = False
        await subscription.dispose()

    def _on_query_change(self, change: QueryChangeSet) -> None:
        current = self._require_data()
        self._data = apply_query_change(current, change)

        for event in change.events:
            if isinstance(event, QueryInsertEvent):
                self.dispatch("on_insert", event)
                continue

            if isinstance(event, QueryUpdateEvent):
                self.dispatch("on_update", event)
                continue

            self.dispatch("on_remove", event)

    def _on_sync_change(
        self,
        synchronized: bool,
        reason: str,
    ) -> None:
        previous = self._synchronized
        self._synchronized = synchronized

        if self._loading or not self.loaded:
            return

        event_name = (
            "on_resynchronized"
            if synchronized
            else "on_sync_lost"
        )

        if previous == synchronized:
            return

        try:
            self.dispatch(event_name, reason)
        except Exception:
            logger.exception(
                "Query adapter synchronization callback failed"
            )

    def _require_data(self) -> QueryResult:
        if self._data is None:
            raise StoreStateError(
                "Query adapter has not been loaded"
            )

        return self._data

    def _require_subscription(self) -> RemoteQuerySubscription:
        subscription = self._subscription

        if subscription is None:
            raise StoreStateError(
                "Query adapter has not been loaded"
            )

        return subscription

    @property
    def store(self) -> RemoteStore:
        return self._store

    @property
    def query(self) -> Query:
        return self._query

    @property
    def data(self) -> Mapping[str, Mapping[str, Any]]:
        return MappingProxyType(self._require_data())

    @property
    def loaded(self) -> bool:
        return self._data is not None

    @property
    def synchronized(self) -> bool:
        subscription = self._subscription

        return (
            self.loaded
            and self._synchronized
            and subscription is not None
            and subscription.synchronized
        )

    @property
    def server_instance_id(self) -> str | None:
        subscription = self._subscription

        if subscription is None:
            return None

        return subscription.server_instance_id

    @property
    def revision(self) -> int | None:
        subscription = self._subscription

        if subscription is None:
            return None

        return subscription.sequence

    def on_loaded(self) -> None:
        pass

    def on_insert(self, event: QueryInsertEvent) -> None:
        pass

    def on_update(self, event: QueryUpdateEvent) -> None:
        pass

    def on_remove(self, event: QueryRemoveEvent) -> None:
        pass

    def on_sync_lost(self, reason: str) -> None:
        pass

    def on_resynchronized(self, reason: str) -> None:
        pass
