# kivyfn/store/adapter/single.py
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
from kivyfn.store.events.event import SetEvent
from kivyfn.store.remote.store import RemoteStore
from kivyfn.store.remote.subscription import (
    RemoteSubscription,
)
from kivyfn.store.response import Response
from kivyfn.store.transaction import TransactionResult


ModelT = TypeVar(
    "ModelT",
    bound=Mapping[str, Any],
)


class Adapter(
    EventDispatcher,
    Generic[ModelT],
):
    """
    Keep one many=False schema synchronized with a remote store.

    load() must complete before data or set() is used. SET subscription
    events are the source of truth, so set() does not update local data
    optimistically.
    """

    __events__ = (
        "on_loaded",
        "on_set",
        "on_set_failed",
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

        self._subscription: (
            RemoteSubscription[SetEvent]
            | None
        ) = None

    # -------------------------------------------------
    # Loading
    # -------------------------------------------------

    async def load(self) -> None:
        if self._data is not None:
            raise StoreStateError(
                f"Adapter for {self.address} is already loaded"
            )

        if self._subscription is not None:
            raise StoreStateError(
                f"Adapter for {self.address} is already loading"
            )

        response = await self.store.subscription.set(
            callback=self._on_store_set,
            address=self.address,
        )

        if response.ok is False:
            raise StoreStateError(
                f"Failed to subscribe adapter for "
                f"{self.address}: {response.error}"
            )

        self._subscription = response.data

        snapshot_response = await self.store.snapshot(
            self.address,
        )

        if snapshot_response.ok is False:
            raise StoreStateError(
                f"Failed to load adapter for "
                f"{self.address}: {snapshot_response.error}"
            )

        snapshot = snapshot_response.data

        if not isinstance(
            snapshot,
            Mapping,
        ):
            raise StoreStateError(
                f"Snapshot for {self.address} must be a mapping"
            )

        self._data = cast(
            ModelT,
            dict(snapshot),
        )

        self.dispatch(
            "on_loaded",
        )

    # -------------------------------------------------
    # Commands
    # -------------------------------------------------

    async def set(
        self,
        key: str,
        value: Any,
    ) -> Response[TransactionResult]:
        current = self._require_data()

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
        current = self._require_data()

        updated = dict(
            current,
        )

        updated[event.key] = event.new_value

        self._data = cast(
            ModelT,
            updated,
        )

        self.dispatch(
            "on_set",
            event,
        )

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
