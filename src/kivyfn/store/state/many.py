from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Generic, TypeVar
from weakref import WeakValueDictionary

from kivy.event import EventDispatcher
from kivy.properties import DictProperty

from kivyfn.store.adapter.many import ManyAdapter
from kivyfn.store.address import Address
from kivyfn.store.error import StoreStateError
from kivyfn.store.events.event import (
    InsertEvent,
    RemoveEvent,
    SetRowEvent,
)
from kivyfn.store.remote.store import RemoteStore
from kivyfn.store.response import Response
from kivyfn.store.state.change import StateChange
from kivyfn.store.state.node import NodeState
from kivyfn.store.transaction import TransactionResult


RowT = TypeVar(
    "RowT",
    bound=Mapping[str, Any],
)

StateT = TypeVar(
    "StateT",
    bound=NodeState,
)


class ManyNodeState(
    EventDispatcher,
    Generic[RowT, StateT],
):
    """
    Project one many=True store schema into observable Kivy state.

    ``state`` contains the currently active rows. ``ref()`` returns a stable
    row object that can remain alive across removal and reinsertion while a
    widget or another consumer still holds it.
    """

    state: dict[str, StateT] = DictProperty(
        {},
        rebind=True,
    )

    __events__ = (
        "on_loaded",
        "on_change",
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
        state_type: type[StateT],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)

        self.state = {}

        self._state_type = state_type
        self._references: WeakValueDictionary[
            str,
            StateT,
        ] = WeakValueDictionary()

        self._adapter = ManyAdapter[RowT](
            store=store,
            address=address,
        )

        self._adapter.bind(
            on_loaded=self._on_adapter_loaded,
            on_insert=self._on_adapter_insert,
            on_setrow=self._on_adapter_setrow,
            on_remove=self._on_adapter_remove,
            on_insert_failed=self._on_adapter_insert_failed,
            on_setrow_failed=self._on_adapter_setrow_failed,
            on_merge_failed=self._on_adapter_merge_failed,
            on_remove_failed=self._on_adapter_remove_failed,
            on_difference_failed=self._on_adapter_difference_failed,
            on_sync_lost=self._on_adapter_sync_lost,
            on_resynchronized=(
                self._on_adapter_resynchronized
            ),
        )

    # -------------------------------------------------
    # Loading
    # -------------------------------------------------

    async def load(self) -> None:
        await self._adapter.load()

    # -------------------------------------------------
    # Stable references
    # -------------------------------------------------

    def ref(
        self,
        id: str,
    ) -> StateT:
        active = self.state.get(id)

        if active is not None:
            return active

        retained = self._references.get(id)

        if retained is not None:
            return retained

        state = self._create_state()
        self._references[id] = state

        return state

    def _create_state(self) -> StateT:
        state = self._state_type()

        if not isinstance(state, NodeState):
            raise StoreStateError(
                f"{self._state_type.__name__} must inherit from NodeState"
            )

        return state

    # -------------------------------------------------
    # Commands
    # -------------------------------------------------

    async def insert(
        self,
        value: RowT,
        id: str | None = None,
    ) -> Response[TransactionResult]:
        return await self._adapter.insert(
            value=value,
            id=id,
        )

    async def setrow(
        self,
        id: str,
        key: str,
        value: Any,
    ) -> Response[TransactionResult]:
        return await self._adapter.setrow(
            id=id,
            key=key,
            value=value,
        )

    async def merge(
        self,
        id: str,
        value: Mapping[str, Any],
    ) -> Response[TransactionResult]:
        return await self._adapter.merge(
            id=id,
            value=value,
        )

    async def remove(
        self,
        id: str,
    ) -> Response[TransactionResult]:
        return await self._adapter.remove(
            id=id,
        )

    async def difference(
        self,
        desired: Mapping[str, RowT],
    ) -> Response[TransactionResult]:
        return await self._adapter.difference(
            desired=desired,
        )

    # -------------------------------------------------
    # Adapter events
    # -------------------------------------------------

    def _on_adapter_loaded(
        self,
        adapter: ManyAdapter[RowT],
    ) -> None:
        rows: dict[str, StateT] = {}

        for id, values in adapter.data.items():
            state = self.ref(id)
            state._hydrate(values)
            rows[id] = state

        self.state = rows

        self.dispatch(
            "on_loaded",
        )

    def _on_adapter_insert(
        self,
        _adapter: ManyAdapter[RowT],
        event: InsertEvent[RowT],
    ) -> None:
        change = StateChange(
            kind="insert",
            path=(event.id,),
            old_value=None,
            new_value=event.new_value,
        )

        state = self.ref(event.id)
        state._insert(
            values=event.new_value,
            change=change,
        )

        updated = dict(self.state)
        updated[event.id] = state
        self.state = updated

        self.dispatch(
            "on_change",
            change,
        )

    def _on_adapter_setrow(
        self,
        _adapter: ManyAdapter[RowT],
        event: SetRowEvent,
    ) -> None:
        state = self.state.get(event.id)

        if state is None:
            raise StoreStateError(
                f"State row {event.id!r} does not exist"
            )

        change = StateChange(
            kind="set",
            path=(event.id, event.key),
            old_value=event.old_value,
            new_value=event.new_value,
        )

        state._apply(
            key=event.key,
            value=event.new_value,
            change=change,
        )

        self.dispatch(
            "on_change",
            change,
        )

    def _on_adapter_remove(
        self,
        _adapter: ManyAdapter[RowT],
        event: RemoveEvent[RowT],
    ) -> None:
        state = self.state.get(event.id)

        if state is None:
            raise StoreStateError(
                f"State row {event.id!r} does not exist"
            )

        change = StateChange(
            kind="remove",
            path=(event.id,),
            old_value=event.old_value,
            new_value=None,
        )

        state._remove(
            change,
        )

        updated = dict(self.state)
        updated.pop(event.id)
        self.state = updated

        self.dispatch(
            "on_change",
            change,
        )

    # -------------------------------------------------
    # Adapter failures
    # -------------------------------------------------

    def _on_adapter_insert_failed(
        self,
        _adapter: ManyAdapter[RowT],
        id: str | None,
        value: RowT,
        error: Any,
    ) -> None:
        self.dispatch(
            "on_insert_failed",
            id,
            value,
            error,
        )

    def _on_adapter_setrow_failed(
        self,
        _adapter: ManyAdapter[RowT],
        id: str,
        key: str,
        previous_value: Any,
        error: Any,
    ) -> None:
        self.dispatch(
            "on_setrow_failed",
            id,
            key,
            previous_value,
            error,
        )

    def _on_adapter_merge_failed(
        self,
        _adapter: ManyAdapter[RowT],
        id: str,
        previous_value: RowT | None,
        error: Any,
    ) -> None:
        self.dispatch(
            "on_merge_failed",
            id,
            previous_value,
            error,
        )

    def _on_adapter_remove_failed(
        self,
        _adapter: ManyAdapter[RowT],
        id: str,
        previous_value: RowT | None,
        error: Any,
    ) -> None:
        self.dispatch(
            "on_remove_failed",
            id,
            previous_value,
            error,
        )

    def _on_adapter_difference_failed(
        self,
        _adapter: ManyAdapter[RowT],
        desired: Mapping[str, RowT],
        error: Any,
    ) -> None:
        self.dispatch(
            "on_difference_failed",
            desired,
            error,
        )

    def _on_adapter_sync_lost(
        self,
        _adapter: ManyAdapter[RowT],
        reason: str,
    ) -> None:
        self.dispatch(
            "on_sync_lost",
            reason,
        )

    def _on_adapter_resynchronized(
        self,
        adapter: ManyAdapter[RowT],
        reason: str,
    ) -> None:
        rows: dict[str, StateT] = {}
        current_ids = set(adapter.data)

        for id, state in self.state.items():
            if id not in current_ids:
                state.exists = False

        for id, values in adapter.data.items():
            state = self.ref(id)
            state._hydrate(values)
            rows[id] = state

        self.state = rows
        self.dispatch(
            "on_resynchronized",
            reason,
        )

    # -------------------------------------------------
    # State
    # -------------------------------------------------

    @property
    def store(self) -> RemoteStore:
        return self._adapter.store

    @property
    def address(self) -> Address[dict[str, RowT]]:
        return self._adapter.address

    # -------------------------------------------------
    # Events
    # -------------------------------------------------

    def on_loaded(self) -> None:
        pass

    def on_change(
        self,
        change: StateChange,
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
