# kivyfn/store/adapter/single.py
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
from kivyfn.store.events.event import SetEvent
from kivyfn.store.remote.store import RemoteStore
from kivyfn.store.remote.subscription import RemoteSubscription
from kivyfn.store.remote.sync import SyncSnapshot
from kivyfn.store.response import Response
from kivyfn.store.transaction import TransactionResult


logger = get_logger(__name__)


ModelT = TypeVar(
    "ModelT",
    bound=Mapping[str, Any],
)


class Adapter(
    EventDispatcher,
    Generic[ModelT],
):
    """Keep one many=False schema synchronized with a remote store."""

    __events__ = (
        "on_loaded",
        "on_set",
        "on_set_failed",
        "on_sync_lost",
        "on_resynchronized",
    )

    def __init__(
        self,
        store: RemoteStore,
        address: Address[ModelT],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)

        self._store = store
        self._address = address
        self._data: ModelT | None = None
        self._subscription: RemoteSubscription[SetEvent] | None = None
        self._loading = False
        self._synchronizing = False
        self._synchronized = False
        self._server_instance_id: str | None = None
        self._revision: int | None = None
        self._event_buffer: list[SetEvent] = []
        self._sync_lock = asyncio.Lock()

    # -------------------------------------------------
    # Loading
    # -------------------------------------------------

    async def load(self) -> None:
        if self.loaded:
            raise StoreStateError(
                f"Adapter for {self.address} is already loaded"
            )

        if self._loading:
            raise StoreStateError(
                f"Adapter for {self.address} is already loading"
            )

        self._loading = True
        self._synchronizing = True
        self._event_buffer.clear()
        subscription: RemoteSubscription[SetEvent] | None = None

        try:
            response = await self.store.subscription.set(
                callback=self._on_store_set,
                address=self.address,
            )

            if response.ok is False:
                raise StoreStateError(
                    "Failed to subscribe adapter for "
                    f"{self.address}: {response.error}"
                )

            subscription = response.data
            self._subscription = subscription
            await self._synchronize_from_store(
                previous=None,
                reason="initial-load",
            )
            self.store.client.register_adapter(
                self,
            )

        except BaseException:
            if subscription is not None:
                try:
                    await subscription.dispose()
                except Exception:
                    logger.exception(
                        "Could not roll back adapter subscription for %s",
                        self.address,
                    )

            self._subscription = None
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

    async def resync(
        self,
        reason: str = "manual",
    ) -> None:
        if not self.loaded:
            return

        async with self._sync_lock:
            previous = cast(
                ModelT,
                dict(self._require_data()),
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
        previous: ModelT | None,
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
    ) -> ModelT:
        if not isinstance(snapshot.data, Mapping):
            raise StoreStateError(
                f"Snapshot for {self.address} must be a mapping"
            )

        return cast(
            ModelT,
            dict(snapshot.data),
        )

    def _replay_buffer(
        self,
        events: tuple[SetEvent, ...],
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

    async def set(
        self,
        key: str,
        value: Any,
    ) -> Response[TransactionResult]:
        current = self._require_synchronized_data()
        previous_value = current.get(
            key,
        )
        transaction = self.store.transaction()
        transaction.set(
            address=self.address,
            key=key,
            value=value,
        )
        response = await self.store.commit(
            transaction,
        )

        if response.ok is False:
            self.dispatch(
                "on_set_failed",
                key,
                previous_value,
                response.error,
            )

        return response

    # -------------------------------------------------
    # Store events
    # -------------------------------------------------

    def _on_store_set(
        self,
        event: SetEvent,
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
        event: SetEvent,
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
        event: SetEvent,
        *,
        dispatch: bool,
    ) -> None:
        current = self._require_data()
        updated = dict(
            current,
        )
        updated[event.key] = event.new_value
        self._data = cast(
            ModelT,
            updated,
        )

        if dispatch:
            self.dispatch(
                "on_set",
                event,
            )

    def _dispatch_reconciliation(
        self,
        previous: ModelT,
    ) -> None:
        current = self._require_data()

        for key in dict.fromkeys((*previous, *current)):
            old_value = previous.get(
                key,
            )
            new_value = current.get(
                key,
            )

            if old_value == new_value:
                continue

            self.dispatch(
                "on_set",
                SetEvent(
                    address=self.address,
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
    def address(self) -> Address[ModelT]:
        return self._address

    @property
    def data(self) -> ModelT:
        return cast(
            ModelT,
            MappingProxyType(
                self._require_data(),
            ),
        )

    def _require_data(self) -> ModelT:
        if self._data is None:
            raise StoreStateError(
                f"Adapter for {self.address} has not been loaded"
            )

        return self._data

    def _require_synchronized_data(self) -> ModelT:
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

    def on_set(
        self,
        event: SetEvent,
    ) -> None:
        pass

    def on_set_failed(
        self,
        key: str,
        previous_value: Any,
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
