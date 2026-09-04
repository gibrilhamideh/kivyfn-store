# kivyfn/store/adapter/many.py
from __future__ import annotations

import asyncio

from collections.abc import Mapping
from types import MappingProxyType
from typing import (
    Any,
    Generic,
    TypeVar,
    cast,
)

from kivy.event import EventDispatcher

from kivyfn.logging import get_logger
from kivyfn.store.address import Address
from kivyfn.store.error import StoreStateError
from kivyfn.store.events.event import (
    InsertEvent,
    RemoveEvent,
    SetRowEvent,
    StoreEvent,
)
from kivyfn.store.remote.store import RemoteStore
from kivyfn.store.remote.subscription import RemoteSubscription
from kivyfn.store.remote.sync import SyncSnapshot
from kivyfn.store.response import Response
from kivyfn.store.transaction import TransactionResult


logger = get_logger(__name__)


RowT = TypeVar(
    "RowT",
    bound=Mapping[str, Any],
)


class ManyAdapter(
    EventDispatcher,
    Generic[RowT],
):
    """Keep one many=True schema synchronized with a remote store."""

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
        "on_sync_lost",
        "on_resynchronized",
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
        self._loading = False
        self._synchronizing = False
        self._synchronized = False
        self._server_instance_id: str | None = None
        self._revision: int | None = None
        self._event_buffer: list[StoreEvent] = []
        self._sync_lock = asyncio.Lock()

    # -------------------------------------------------
    # Loading
    # -------------------------------------------------

    async def load(self) -> None:
        if self.loaded:
            raise StoreStateError(
                f"Adapter for {self.address} is already loaded"
            )

        if self._loading or self._subscriptions:
            raise StoreStateError(
                f"Adapter for {self.address} is already loading"
            )

        self._loading = True
        self._synchronizing = True
        self._event_buffer.clear()

        try:
            await self._subscribe()
            await self._synchronize_from_store(
                previous=None,
                reason="initial-load",
            )
            self.store.client.register_adapter(
                self,
            )

        except BaseException:
            await self._rollback_subscriptions()
            self._data = None
            self._synchronized = False
            self._synchronizing = False
            self._server_instance_id = None
            self._revision = None
            self._event_buffer.clear()
            raise

        finally:
            self._loading = False

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
                "Failed to subscribe to inserts for "
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
                "Failed to subscribe to row changes for "
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
                "Failed to subscribe to removals for "
                f"{self.address}: {remove_response.error}"
            )

        self._subscriptions.append(
            remove_response.data,
        )

    async def _rollback_subscriptions(self) -> None:
        subscriptions = tuple(
            self._subscriptions,
        )
        self._subscriptions.clear()

        for subscription in subscriptions:
            try:
                await subscription.dispose()
            except Exception:
                logger.exception(
                    "Could not roll back adapter subscription for %s",
                    self.address,
                )

    async def resync(
        self,
        reason: str = "manual",
    ) -> None:
        if not self.loaded:
            return

        async with self._sync_lock:
            previous = self._copy_data(
                self._require_data(),
            )
            self.mark_sync_lost(
                reason,
            )
            self._synchronizing = True

            try:
                await self._synchronize_from_store(
                    previous=previous,
                    reason=reason,
                )
            except BaseException:
                self._synchronizing = False
                self._synchronized = False
                raise

        logger.info(
            "Store adapter resynchronized: address=%s revision=%s "
            "reason=%s",
            self.address,
            self.revision,
            reason,
        )

    async def _synchronize_from_store(
        self,
        *,
        previous: dict[str, RowT] | None,
        reason: str,
    ) -> None:
        previous_instance_id = self._server_instance_id
        previous_revision = self._revision

        for _attempt in range(8):
            response = await self.store.sync_snapshot(
                self.address,
            )

            if response.ok is False:
                raise StoreStateError(
                    "Failed to synchronize adapter for "
                    f"{self.address}: {response.error}"
                )

            snapshot = response.data
            data = self._copy_snapshot(
                snapshot,
            )
            pending = tuple(
                self._event_buffer,
            )
            self._event_buffer.clear()
            self._data = data
            self._server_instance_id = snapshot.server_instance_id
            self._revision = snapshot.revision

            if self._replay_buffer(pending):
                self._synchronizing = False
                self._synchronized = True

                if previous is not None:
                    try:
                        self._dispatch_reconciliation(
                            previous,
                        )
                    except BaseException:
                        self._data = previous
                        self._server_instance_id = previous_instance_id
                        self._revision = previous_revision
                        self._synchronized = False
                        raise

                    try:
                        self.dispatch(
                            "on_resynchronized",
                            reason,
                        )
                    except Exception:
                        logger.exception(
                            "Store adapter resynchronized callback failed "
                            "for %s",
                            self.address,
                        )

                return

        if previous is not None:
            self._data = previous
            self._server_instance_id = previous_instance_id
            self._revision = previous_revision

        raise StoreStateError(
            "Adapter could not obtain a contiguous event stream for "
            f"{self.address}"
        )

    def _copy_snapshot(
        self,
        snapshot: SyncSnapshot[Any],
    ) -> dict[str, RowT]:
        if not isinstance(snapshot.data, Mapping):
            raise StoreStateError(
                f"Snapshot for {self.address} must be a mapping"
            )

        return self._copy_data(
            snapshot.data,
        )

    @staticmethod
    def _copy_data(
        value: Mapping[str, Any],
    ) -> dict[str, RowT]:
        copied: dict[str, RowT] = {}

        for id, row in value.items():
            if not isinstance(id, str):
                raise StoreStateError(
                    "Many-adapter snapshot IDs must be strings"
                )

            if not isinstance(row, Mapping):
                raise StoreStateError(
                    "Many-adapter snapshot rows must be mappings"
                )

            copied[id] = cast(
                RowT,
                dict(row),
            )

        return copied

    def _replay_buffer(
        self,
        events: tuple[StoreEvent, ...],
    ) -> bool:
        ordered = sorted(
            events,
            key=lambda event: (
                event.revision
                if event.revision is not None
                else -1
            ),
        )

        for event in ordered:
            if event.server_instance_id != self._server_instance_id:
                continue

            if event.revision is None or self._revision is None:
                return False

            if event.revision <= self._revision:
                continue

            if event.revision != self._revision + 1:
                return False

            try:
                self._apply_event(
                    event,
                    dispatch=False,
                )
            except Exception:
                return False

            self._revision = event.revision

        return True

    # -------------------------------------------------
    # Commands
    # -------------------------------------------------

    async def insert(
        self,
        value: RowT,
        id: str | None = None,
    ) -> Response[TransactionResult]:
        self._require_synchronized_data()
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
        current = self._require_synchronized_data()
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
        current = self._require_synchronized_data()
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
        current = self._require_synchronized_data()
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
        current = self._require_synchronized_data()
        current_ids = set(current)
        desired_ids = set(desired)
        transaction = self.store.transaction()

        for id in current_ids - desired_ids:
            transaction.remove(
                address=self.address,
                id=id,
            )

        for id in desired_ids - current_ids:
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
        self._receive_event(
            event,
        )

    def _on_store_setrow(
        self,
        event: SetRowEvent,
    ) -> None:
        self._receive_event(
            event,
        )

    def _on_store_remove(
        self,
        event: RemoveEvent[RowT],
    ) -> None:
        self._receive_event(
            event,
        )

    def _receive_event(
        self,
        event: StoreEvent,
    ) -> None:
        if self._synchronizing or not self._synchronized:
            self._event_buffer.append(
                event,
            )
            return

        if (
            event.server_instance_id == self._server_instance_id
            and event.revision is not None
            and self._revision is not None
            and event.revision <= self._revision
        ):
            return

        if not self._event_is_next(event):
            self._event_buffer.append(
                event,
            )
            self.store.client.request_adapter_resync(
                self,
                "event-revision-gap",
            )
            return

        previous = self._data

        try:
            self._apply_event(
                event,
                dispatch=True,
            )
        except Exception:
            self._data = previous
            self._event_buffer.append(
                event,
            )
            self.mark_sync_lost(
                "event-apply-failed",
            )
            raise

        self._revision = event.revision

    def _event_is_next(
        self,
        event: StoreEvent,
    ) -> bool:
        if (
            event.server_instance_id is None
            or event.revision is None
            or self._server_instance_id is None
            or self._revision is None
        ):
            return False

        if event.server_instance_id != self._server_instance_id:
            return False

        if event.revision <= self._revision:
            return False

        return event.revision == self._revision + 1

    def _apply_event(
        self,
        event: StoreEvent,
        *,
        dispatch: bool,
    ) -> None:
        if isinstance(event, InsertEvent):
            self._apply_insert(
                event,
                dispatch=dispatch,
            )
            return

        if isinstance(event, SetRowEvent):
            self._apply_setrow(
                event,
                dispatch=dispatch,
            )
            return

        if isinstance(event, RemoveEvent):
            self._apply_remove(
                event,
                dispatch=dispatch,
            )
            return

        raise StoreStateError(
            f"Unsupported event for many adapter: {event.opcode}"
        )

    def _apply_insert(
        self,
        event: InsertEvent[Any],
        *,
        dispatch: bool,
    ) -> None:
        current = self._require_data()

        if event.id in current:
            raise StoreStateError(
                "Received insertion for existing id "
                f"{event.id!r} at {self.address}"
            )

        if not isinstance(event.new_value, Mapping):
            raise StoreStateError(
                "Received insertion with a non-mapping row at "
                f"{self.address}"
            )

        updated = dict(
            current,
        )
        updated[event.id] = cast(
            RowT,
            dict(event.new_value),
        )
        self._data = updated

        if dispatch:
            self.dispatch(
                "on_insert",
                event,
            )

    def _apply_setrow(
        self,
        event: SetRowEvent,
        *,
        dispatch: bool,
    ) -> None:
        current = self._require_data()
        row = current.get(
            event.id,
        )

        if row is None:
            raise StoreStateError(
                "Received row change for unknown id "
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

        if dispatch:
            self.dispatch(
                "on_setrow",
                event,
            )

    def _apply_remove(
        self,
        event: RemoveEvent[Any],
        *,
        dispatch: bool,
    ) -> None:
        current = self._require_data()

        if event.id not in current:
            raise StoreStateError(
                "Received removal for unknown id "
                f"{event.id!r} at {self.address}"
            )

        updated = dict(
            current,
        )
        updated.pop(
            event.id,
        )
        self._data = updated

        if dispatch:
            self.dispatch(
                "on_remove",
                event,
            )

    def _dispatch_reconciliation(
        self,
        previous: dict[str, RowT],
    ) -> None:
        current = self._require_data()

        for id in previous:
            if id in current:
                continue

            self.dispatch(
                "on_remove",
                RemoveEvent(
                    address=self.address,
                    id=id,
                    old_value=previous[id],
                    server_instance_id=self._server_instance_id,
                    revision=self._revision,
                ),
            )

        for id in current:
            if id in previous:
                continue

            self.dispatch(
                "on_insert",
                InsertEvent(
                    address=self.address,
                    id=id,
                    new_value=current[id],
                    server_instance_id=self._server_instance_id,
                    revision=self._revision,
                ),
            )

        for id in current:
            if id not in previous:
                continue

            old_row = previous[id]
            new_row = current[id]

            for key in dict.fromkeys((*old_row, *new_row)):
                old_value = old_row.get(
                    key,
                )
                new_value = new_row.get(
                    key,
                )

                if old_value == new_value:
                    continue

                self.dispatch(
                    "on_setrow",
                    SetRowEvent(
                        address=self.address,
                        id=id,
                        key=key,
                        old_value=old_value,
                        new_value=new_value,
                        server_instance_id=self._server_instance_id,
                        revision=self._revision,
                    ),
                )

    # -------------------------------------------------
    # Synchronization state
    # -------------------------------------------------

    def mark_sync_lost(
        self,
        reason: str,
    ) -> None:
        was_synchronized = self._synchronized
        self._synchronized = False

        if was_synchronized:
            try:
                self.dispatch(
                    "on_sync_lost",
                    reason,
                )
            except Exception:
                logger.exception(
                    "Store adapter sync-lost callback failed for %s",
                    self.address,
                )

    @property
    def loaded(self) -> bool:
        return self._data is not None

    @property
    def synchronized(self) -> bool:
        return self.loaded and self._synchronized

    @property
    def server_instance_id(self) -> str | None:
        return self._server_instance_id

    @property
    def revision(self) -> int | None:
        return self._revision

    # -------------------------------------------------
    # State
    # -------------------------------------------------

    @property
    def store(self) -> RemoteStore:
        return self._store

    @property
    def address(self) -> Address[dict[str, RowT]]:
        return self._address

    @property
    def data(self) -> Mapping[str, RowT]:
        return MappingProxyType(
            self._require_data(),
        )

    def _require_data(self) -> dict[str, RowT]:
        if self._data is None:
            raise StoreStateError(
                f"Adapter for {self.address} has not been loaded"
            )

        return self._data

    def _require_synchronized_data(self) -> dict[str, RowT]:
        data = self._require_data()

        if not self._synchronized:
            raise StoreStateError(
                f"Adapter for {self.address} is not synchronized"
            )

        return data

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

    def on_sync_lost(
        self,
        reason: str,
    ) -> None:
        pass

    def on_resynchronized(
        self,
        reason: str,
    ) -> None:
        pass
