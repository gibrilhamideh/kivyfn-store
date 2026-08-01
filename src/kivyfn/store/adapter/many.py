# kivyfn/store/adapter/many.py
from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import (
    Any,
    Generic,
    TypeVar,
    cast,
)

from kivy.event import EventDispatcher

from kivyfn.store.address import Address
from kivyfn.store.error import StoreStateError
from kivyfn.store.events.event import (
    InsertEvent,
    RemoveEvent,
    SetRowEvent,
)
from kivyfn.store.remote.store import RemoteStore
from kivyfn.store.remote.subscription import (
    RemoteSubscription,
)
from kivyfn.store.response import Response
from kivyfn.store.transaction import TransactionResult


RowT = TypeVar(
    "RowT",
    bound=Mapping[str, Any],
)


class ManyAdapter(
    EventDispatcher,
    Generic[RowT],
):
    """
    Keep one many=True schema synchronized with a remote store.

    load() must complete before data or mutation helpers are used. INSERT,
    SETROW, and REMOVE subscription events are the source of truth, so
    mutation helpers do not update local data optimistically.
    """

    __events__ = (
        "on_loaded",
        "on_insert",
        "on_setrow",
        "on_remove",
        "on_insert_failed",
        "on_setrow_failed",
        "on_merge_failed",
        "on_remove_failed",
        "on_difference_failed",
    )

    def __init__(
        self,
        store: RemoteStore,
        address: Address[dict[str, RowT]],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)

        self._store = store
        self._address = address

        self._data: dict[str, RowT] | None = None

        self._subscriptions: list[
            RemoteSubscription[Any]
        ] = []

    # -------------------------------------------------
    # Loading
    # -------------------------------------------------

    async def load(self) -> None:
        if self._data is not None:
            raise StoreStateError(
                f"Adapter for {self.address} is already loaded"
            )

        if self._subscriptions:
            raise StoreStateError(
                f"Adapter for {self.address} is already loading"
            )

        await self._subscribe()

        response = await self.store.snapshot(
            self.address,
        )

        if response.ok is False:
            raise StoreStateError(
                f"Failed to load adapter for "
                f"{self.address}: {response.error}"
            )

        snapshot = response.data

        if not isinstance(
            snapshot,
            Mapping,
        ):
            raise StoreStateError(
                f"Snapshot for {self.address} must be a mapping"
            )

        self._data = cast(
            dict[str, RowT],
            {
                id: cast(
                    RowT,
                    dict(row),
                )
                for id, row in snapshot.items()
            },
        )

        self.dispatch(
            "on_loaded",
        )

    async def _subscribe(self) -> None:
        insert_response = await self.store.subscription.insert(
            callback=self._on_store_insert,
            address=self.address,
        )

        if insert_response.ok is False:
            raise StoreStateError(
                f"Failed to subscribe to inserts for "
                f"{self.address}: {insert_response.error}"
            )

        self._subscriptions.append(
            insert_response.data,
        )

        setrow_response = await self.store.subscription.setrow(
            callback=self._on_store_setrow,
            address=self.address,
        )

        if setrow_response.ok is False:
            raise StoreStateError(
                f"Failed to subscribe to row changes for "
                f"{self.address}: {setrow_response.error}"
            )

        self._subscriptions.append(
            setrow_response.data,
        )

        remove_response = await self.store.subscription.remove(
            callback=self._on_store_remove,
            address=self.address,
        )

        if remove_response.ok is False:
            raise StoreStateError(
                f"Failed to subscribe to removals for "
                f"{self.address}: {remove_response.error}"
            )

        self._subscriptions.append(
            remove_response.data,
        )

    # -------------------------------------------------
    # Commands
    # -------------------------------------------------

    async def insert(
        self,
        value: RowT,
        id: str | None = None,
    ) -> Response[TransactionResult]:
        self._require_data()

        transaction = self.store.transaction()

        transaction.insert(
            address=self.address,
            value=dict(value),
            id=id,
        )

        response = await self.store.commit(
            transaction,
        )

        if response.ok is False:
            self.dispatch(
                "on_insert_failed",
                id,
                value,
                response.error,
            )

        return response

    async def setrow(
        self,
        id: str,
        key: str,
        value: Any,
    ) -> Response[TransactionResult]:
        current = self._require_data()

        row = current.get(
            id,
        )

        previous_value = (
            row.get(key)
            if row is not None
            else None
        )

        transaction = self.store.transaction()

        transaction.setrow(
            address=self.address,
            id=id,
            key=key,
            value=value,
        )

        response = await self.store.commit(
            transaction,
        )

        if response.ok is False:
            self.dispatch(
                "on_setrow_failed",
                id,
                key,
                previous_value,
                response.error,
            )

        return response

    async def merge(
        self,
        id: str,
        value: Mapping[str, Any],
    ) -> Response[TransactionResult]:
        current = self._require_data()

        row = current.get(
            id,
        )

        previous_value = (
            cast(
                RowT,
                dict(row),
            )
            if row is not None
            else None
        )

        transaction = self.store.transaction()

        transaction.merge(
            address=self.address,
            id=id,
            value=dict(value),
        )

        response = await self.store.commit(
            transaction,
        )

        if response.ok is False:
            self.dispatch(
                "on_merge_failed",
                id,
                previous_value,
                response.error,
            )

        return response

    async def remove(
        self,
        id: str,
    ) -> Response[TransactionResult]:
        current = self._require_data()

        row = current.get(
            id,
        )

        previous_value = (
            cast(
                RowT,
                dict(row),
            )
            if row is not None
            else None
        )

        transaction = self.store.transaction()

        transaction.remove(
            address=self.address,
            id=id,
        )

        response = await self.store.commit(
            transaction,
        )

        if response.ok is False:
            self.dispatch(
                "on_remove_failed",
                id,
                previous_value,
                response.error,
            )

        return response

    async def difference(
        self,
        desired: Mapping[str, RowT],
    ) -> Response[TransactionResult]:
        current = self._require_data()

        current_ids = set(current)
        desired_ids = set(desired)

        remove_ids = current_ids - desired_ids
        insert_ids = desired_ids - current_ids

        transaction = self.store.transaction()

        for id in remove_ids:
            transaction.remove(
                address=self.address,
                id=id,
            )

        for id in insert_ids:
            transaction.insert(
                address=self.address,
                id=id,
                value=dict(desired[id]),
            )

        response = await self.store.commit(
            transaction,
        )

        if response.ok is False:
            self.dispatch(
                "on_difference_failed",
                desired,
                response.error,
            )

        return response

    # -------------------------------------------------
    # Store events
    # -------------------------------------------------

    def _on_store_insert(
        self,
        event: InsertEvent[RowT],
    ) -> None:
        current = self._require_data()

        if event.id in current:
            raise StoreStateError(
                f"Received insertion for existing id "
                f"{event.id!r} at {self.address}"
            )

        updated = dict(
            current,
        )

        updated[event.id] = cast(
            RowT,
            dict(event.new_value),
        )

        self._data = updated

        self.dispatch(
            "on_insert",
            event,
        )

    def _on_store_setrow(
        self,
        event: SetRowEvent,
    ) -> None:
        current = self._require_data()

        row = current.get(
            event.id,
        )

        if row is None:
            raise StoreStateError(
                f"Received row change for unknown id "
                f"{event.id!r} at {self.address}"
            )

        updated_row = dict(
            row,
        )

        updated_row[event.key] = event.new_value

        updated = dict(
            current,
        )

        updated[event.id] = cast(
            RowT,
            updated_row,
        )

        self._data = updated

        self.dispatch(
            "on_setrow",
            event,
        )

    def _on_store_remove(
        self,
        event: RemoveEvent[RowT],
    ) -> None:
        current = self._require_data()

        if event.id not in current:
            raise StoreStateError(
                f"Received removal for unknown id "
                f"{event.id!r} at {self.address}"
            )

        updated = dict(
            current,
        )

        updated.pop(
            event.id,
        )

        self._data = updated

        self.dispatch(
            "on_remove",
            event,
        )

    # -------------------------------------------------
    # State
    # -------------------------------------------------

    @property
    def store(self) -> RemoteStore:
        return self._store

    @property
    def address(
        self,
    ) -> Address[dict[str, RowT]]:
        return self._address

    @property
    def data(self) -> Mapping[str, RowT]:
        return MappingProxyType(
            self._require_data(),
        )

    def _require_data(
        self,
    ) -> dict[str, RowT]:
        if self._data is None:
            raise StoreStateError(
                f"Adapter for {self.address} has not been loaded"
            )

        return self._data

    # -------------------------------------------------
    # Events
    # -------------------------------------------------

    def on_loaded(self) -> None:
        pass

    def on_insert(
        self,
        event: InsertEvent[RowT],
    ) -> None:
        pass

    def on_setrow(
        self,
        event: SetRowEvent,
    ) -> None:
        pass

    def on_remove(
        self,
        event: RemoveEvent[RowT],
    ) -> None:
        pass

    def on_insert_failed(
        self,
        id: str | None,
        value: RowT,
        error: Any,
    ) -> None:
        pass

    def on_setrow_failed(
        self,
        id: str,
        key: str,
        previous_value: Any,
        error: Any,
    ) -> None:
        pass

    def on_merge_failed(
        self,
        id: str,
        previous_value: RowT | None,
        error: Any,
    ) -> None:
        pass

    def on_remove_failed(
        self,
        id: str,
        previous_value: RowT | None,
        error: Any,
    ) -> None:
        pass

    def on_difference_failed(
        self,
        desired: Mapping[str, RowT],
        error: Any,
    ) -> None:
        pass
