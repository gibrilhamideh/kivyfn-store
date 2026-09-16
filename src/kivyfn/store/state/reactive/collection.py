# src/kivyfn/store/state/reactive/collection.py
from __future__ import annotations

from collections.abc import Iterable, Mapping
from functools import partial
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from kivy.properties import BooleanProperty, NumericProperty

from kivyfn.store.protocol.message.event import (
    InsertEventAction,
    RemoveEventAction,
)
from kivyfn.store.schema import ManySchema
from kivyfn.store.state.reactive.base import (
    BaseCollection,
    RowFactory,
    TransactionStatus,
)
from kivyfn.store.state.reactive.data import CollectionData, StateData
from kivyfn.store.state.reactive.dispatch import (
    dispatch,
    notification_batch,
    notify,
    set_property,
)
from kivyfn.store.state.reactive.state import State

if TYPE_CHECKING:
    from kivyfn.store.state.transaction.failure import TransactionFailure
    from kivyfn.store.state.transaction.transaction import StateTransaction

SchemaT = TypeVar("SchemaT", bound=ManySchema)


class CollectionManagerProtocol(Protocol):
    def request_insert(
        self,
        collection: CollectionState[Any],
        row_id: str,
        values: dict[str, Any],
        *,
        optimistic: bool,
    ) -> StateTransaction: ...

    def request_remove(
        self,
        collection: CollectionState[Any],
        row_id: str,
        *,
        optimistic: bool,
    ) -> StateTransaction: ...


class CollectionState(BaseCollection[SchemaT]):
    """Editable reactive keyed collection of State objects."""

    is_pending = BooleanProperty(False)
    pending_count = NumericProperty(0)
    __events__ = (*TransactionStatus.events, "on_insert", "on_remove")

    def __init__(
        self,
        manager: CollectionManagerProtocol,
        schema: SchemaT,
        row_factory: RowFactory[SchemaT],
    ) -> None:
        data: CollectionData[State[SchemaT]] = CollectionData()
        super().__init__(schema, row_factory, data.visible_view)
        self._transaction_status = TransactionStatus()
        self._manager = manager
        self._data = data

    def __getattribute__(self, name: str) -> Any:
        if name in {"pending_count", "is_pending"}:
            try:
                status = object.__getattribute__(self, "_transaction_status")
            except AttributeError:
                pass
            else:
                return len(status) if name == "pending_count" else bool(status)
        return super().__getattribute__(name)

    def __setitem__(self, row_id: str, values: Mapping[str, Any]) -> None:
        if row_id in self:
            raise ValueError(f"Collection row already exists: {row_id!r}")

        self._manager.request_insert(
            self, row_id, self._prepare_values(values), optimistic=True
        )

    def __delitem__(self, row_id: str) -> None:
        if row_id not in self:
            raise KeyError(row_id)

        self._manager.request_remove(self, row_id, optimistic=True)

    def insert(self, row_id: str, **values: Any) -> StateTransaction:
        if row_id in self:
            raise ValueError(f"Collection row already exists: {row_id!r}")

        return self._manager.request_insert(
            self, row_id, self._prepare_values(values), optimistic=False
        )

    def remove(self, row_id: str) -> StateTransaction:
        if row_id not in self:
            raise KeyError(row_id)

        return self._manager.request_remove(self, row_id, optimistic=False)

    def canonical_row(self, row_id: str) -> State[SchemaT] | None:
        """Find a confirmed row even when an optimistic removal hides it."""
        return self._data.canonical(row_id)

    def sync_rows(self, synced: bool) -> None:
        for state in self._data.canonical_snapshot().values():
            state.set_owner_synced(self, synced)
        set_property(self, "synced", synced)

    def set_canonical(
        self, values: Mapping[str, State[SchemaT] | None]
    ) -> None:
        """Patch confirmed rows and own sync and notification effects."""
        self._require_rows(values)
        with notification_batch():
            previous = self._data.canonical_snapshot()
            delta = self._data.set_canonical(values)
            self._sync_owners(previous)
            notify(partial(self.publish, delta))

    def replace_canonical(self, values: Mapping[str, State[SchemaT]]) -> None:
        """Replace confirmed rows while retaining transaction overlays."""
        self._require_rows(values)
        with notification_batch():
            previous = self._data.canonical_snapshot()
            delta = self._data.replace_canonical(values)
            self._sync_owners(previous)
            notify(partial(self.publish, delta))

    def set_overlay(
        self, transaction_id: str, values: Mapping[str, State[SchemaT] | None]
    ) -> None:
        """Store temporary row entries while preserving State identity."""
        self._require_rows(values)
        delta = self._data.set_overlay(transaction_id, values)
        notify(partial(self.publish, delta))

    def discard_overlay(
        self, transaction_id: str, keys: Iterable[str] | None = None
    ) -> None:
        """Remove only this transaction's contribution, then notify changes."""
        delta = self._data.discard_overlay(transaction_id, keys)
        notify(partial(self.publish, delta))

    def apply_insert_overlay(
        self,
        transaction_id: str,
        state: State[SchemaT],
        values: Mapping[str, Any],
    ) -> None:
        """Expose an optimistic row and its values in one update."""
        if state.row_id is None:
            raise ValueError("Insert overlay requires a row ID")
        with notification_batch():
            self.set_overlay(transaction_id, {state.row_id: state})
            state.set_overlay(transaction_id, values)

    def begin_transaction(self, transaction: StateTransaction) -> None:
        self._transaction_status.begin(self, transaction)

    def succeed_transaction(self, transaction: StateTransaction) -> None:
        self._transaction_status.succeed(self, transaction)

    def fail_transaction(
        self, transaction: StateTransaction, failure: TransactionFailure
    ) -> None:
        self._transaction_status.fail(self, transaction, failure)

    def _apply_insert(self, action: InsertEventAction) -> None:
        super()._apply_insert(action)
        state = self._require_row(action.row_id)
        notify(partial(self.publish_insert, action.row_id, state))

    def _apply_remove(self, action: RemoveEventAction) -> None:
        state = self.canonical_row(action.row_id)
        super()._apply_remove(action)
        if state is not None:
            notify(partial(self.publish_remove, action.row_id, state))

    def publish_insert(self, row_id: str, state: State[SchemaT]) -> None:
        dispatch(self, "on_insert", row_id, state)

    def publish_remove(self, row_id: str, state: State[SchemaT]) -> None:
        dispatch(self, "on_remove", row_id, state)

    def publish_signal(
        self, row_id: str, signal: str, data: Any = None
    ) -> None:
        dispatch(self, f"on_{signal}", row_id, StateData.copy(data))

    def _sync_owners(self, previous: Mapping[str, State[SchemaT]]) -> None:
        """Track canonical ownership even when overlays hide membership."""
        current = self._data.canonical_snapshot()
        for row_id, state in previous.items():
            if current.get(row_id) is not state:
                state.set_owner_synced(self, False)
        for state in current.values():
            state.set_owner_synced(self, bool(self.synced))

    def _prepare_values(self, values: Mapping[str, Any]) -> dict[str, Any]:
        copied = StateData.copy_values(values)

        for key, value in copied.items():
            self._schema.challenge_field(key, value)

        return copied

    def on_pending(self, transaction: StateTransaction) -> None:
        pass

    def on_success(self, transaction: StateTransaction) -> None:
        pass

    def on_failed(
        self, transaction: StateTransaction, failure: TransactionFailure
    ) -> None:
        pass

    def on_done(
        self, transaction: StateTransaction, failure: TransactionFailure | None
    ) -> None:
        pass

    def on_insert(self, row_id: str, state: State[SchemaT]) -> None:
        pass

    def on_remove(self, row_id: str, state: State[SchemaT]) -> None:
        pass

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"schema={self._schema.name!r}, "
            f"rows={len(self)}"
            ")"
        )
