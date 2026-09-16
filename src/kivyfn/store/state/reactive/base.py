"""Shared transaction participation and reactive collection behavior.

The manager coordinates transactions and subscription messages. These classes
own participant notifications and collection updates. Notification batches let
an entire message become readable before application callbacks run.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from kivy.event import EventDispatcher
from kivy.properties import AliasProperty, BooleanProperty, NumericProperty

from kivyfn.store.error import StoreStateError
from kivyfn.store.protocol.message.event import (
    EventAction,
    InsertEventAction,
    RemoveEventAction,
    SetRowEventAction,
)
from kivyfn.store.query import QueryRow
from kivyfn.store.schema import ManySchema
from kivyfn.store.state.reactive.data import MembershipDelta as MembershipDelta
from kivyfn.store.state.reactive.dispatch import (
    dispatch,
    notification_batch,
    set_property,
)

if TYPE_CHECKING:
    from kivyfn.store.state.reactive.collection import CollectionState
    from kivyfn.store.state.reactive.state import State
    from kivyfn.store.state.transaction.failure import TransactionFailure
    from kivyfn.store.state.transaction.transaction import StateTransaction

SchemaT = TypeVar("SchemaT", bound=ManySchema)
RowFactory = Callable[[str], "State[SchemaT]"]


class TransactionStatus:
    """Participant lifecycle shared by State and editable collections.

    Tracks transaction IDs and delivers events on the supplied reactive
    object. It does not submit writes or settle the transaction's future.
    Query containers do not own this helper; their editable rows do.
    """

    events = ("on_pending", "on_success", "on_failed", "on_done")

    def __init__(self) -> None:
        self._pending: set[str] = set()

    def __len__(self) -> int:
        return len(self._pending)

    def begin(
        self,
        target: State[Any] | CollectionState[Any],
        transaction: StateTransaction,
    ) -> None:
        """Start participation, allowing pending bindings to abort it."""
        if transaction.done or transaction.id in self._pending:
            return
        self._pending.add(transaction.id)
        self._sync_pending(target)
        # A pending-property callback may have aborted this transaction.
        if transaction.id in self._pending:
            dispatch(target, "on_pending", transaction)

    def succeed(
        self,
        target: State[Any] | CollectionState[Any],
        transaction: StateTransaction,
    ) -> None:
        if not self._finish(target, transaction.id):
            return
        dispatch(target, "on_success", transaction)
        dispatch(target, "on_done", transaction, None)

    def fail(
        self,
        target: State[Any] | CollectionState[Any],
        transaction: StateTransaction,
        failure: TransactionFailure,
    ) -> None:
        if not self._finish(target, transaction.id):
            return
        dispatch(target, "on_failed", transaction, failure)
        dispatch(target, "on_done", transaction, failure)

    def _finish(
        self, target: State[Any] | CollectionState[Any], transaction_id: str
    ) -> bool:
        if transaction_id not in self._pending:
            return False
        self._pending.remove(transaction_id)
        self._sync_pending(target)
        return True

    def _sync_pending(self, target: State[Any] | CollectionState[Any]) -> None:
        # Publish separately so one failing binding cannot block the other.
        set_property(target, "pending_count", len(self))
        set_property(target, "is_pending", bool(self))


class BaseCollection(EventDispatcher, Generic[SchemaT]):
    """Reactive row access, synchronization ownership, and notifications.

    Subclasses supply a read-only row view and implement host membership
    updates. CollectionState owns CollectionData; QueryCollectionState owns
    a plain row dictionary. This base does not choose their storage model.
    """

    synced = BooleanProperty(False)
    _row_version = NumericProperty(0)
    __events__ = ("on_enter", "on_leave")

    def __init__(
        self,
        schema: SchemaT,
        row_factory: RowFactory[SchemaT],
        rows_view: Mapping[str, State[SchemaT]],
    ) -> None:
        super().__init__()
        self._schema = schema
        self._row_factory = row_factory
        self._rows_view = rows_view
        self._event_handlers: dict[type, Callable[[Any], None]] = {
            InsertEventAction: self._apply_insert,
            RemoveEventAction: self._apply_remove,
            SetRowEventAction: self._apply_set,
        }

    @property
    def schema(self) -> SchemaT:
        return self._schema

    def _get_rows(self) -> Mapping[str, State[SchemaT]]:
        return self._rows_view

    rows = AliasProperty(_get_rows, None, bind=("_row_version",))

    def __getitem__(self, row_id: str) -> State[SchemaT]:
        return self._rows_view[row_id]

    def __iter__(self) -> Iterator[str]:
        return iter(self._rows_view)

    def __len__(self) -> int:
        return len(self._rows_view)

    def __contains__(self, row_id: object) -> bool:
        return row_id in self._rows_view

    def get(self, row_id: str) -> State[SchemaT] | None:
        return self._rows_view.get(row_id)

    def keys(self) -> tuple[str, ...]:
        return tuple(self._rows_view)

    def values(self) -> tuple[State[SchemaT], ...]:
        return tuple(self._rows_view.values())

    def items(self) -> tuple[tuple[str, State[SchemaT]], ...]:
        return tuple(self._rows_view.items())

    def canonical_row(self, row_id: str) -> State[SchemaT] | None:
        return self._rows_view.get(row_id)

    def set_synced(self, synced: bool) -> None:
        self.sync_rows(synced)

    def sync_rows(self, synced: bool) -> None:
        for state in self._rows_view.values():
            state.set_owner_synced(self, synced)
        set_property(self, "synced", synced)

    def set_canonical(
        self, values: Mapping[str, State[SchemaT] | None]
    ) -> None:
        """Patch rows using State objects or None for removal."""
        raise NotImplementedError

    def replace_canonical(self, values: Mapping[str, State[SchemaT]]) -> None:
        """Replace confirmed rows with already-resolved State objects."""
        raise NotImplementedError

    def enter(self, row_id: str) -> None:
        self.set_canonical({row_id: self._resolve_row(row_id)})

    def leave(self, row_id: str) -> None:
        self.set_canonical({row_id: None})

    def apply_snapshot(self, rows: tuple[QueryRow, ...]) -> None:
        """Resolve snapshot rows, then install all data before callbacks."""
        row_ids = tuple(row.id for row in rows)
        if len(row_ids) != len(set(row_ids)):
            raise ValueError("Snapshot cannot contain duplicate row IDs")
        with notification_batch():
            states = {row.id: self._resolve_row(row.id) for row in rows}
            self.replace_canonical(states)
            for row in rows:
                states[row.id].set_canonical(row.to_dict())

    def apply_event(self, action: EventAction) -> None:
        """Apply a retained host action; signals are delivered separately."""
        handler = self._event_handlers.get(type(action))
        if handler is None:
            raise TypeError("Unsupported collection event action")
        with notification_batch():
            handler(action)

    def publish(self, delta: MembershipDelta[State[SchemaT]]) -> None:
        """Deliver a stored change after the surrounding update is complete."""
        if not delta.changed:
            return
        set_property(self, "_row_version", self._row_version + 1)
        for row_id, state in delta.left:
            dispatch(self, "on_leave", row_id, state)
        for row_id, state in delta.entered:
            dispatch(self, "on_enter", row_id, state)

    def _apply_set(self, action: SetRowEventAction) -> None:
        self._require_row(action.row_id).set_canonical(dict(action.values))

    def _apply_insert(self, action: InsertEventAction) -> None:
        self.enter(action.row_id)
        self._require_row(action.row_id).set_canonical(dict(action.values))

    def _apply_remove(self, action: RemoveEventAction) -> None:
        self.leave(action.row_id)

    def _require_rows(
        self, values: Mapping[str, State[SchemaT] | None]
    ) -> None:
        """Check row references without creating objects or changing data."""
        for row_id, state in values.items():
            self._validate_row_id(row_id)
            if state is not None and state.row_id != row_id:
                raise StoreStateError("State row ID does not match its key")

    def _resolve_row(self, row_id: str) -> State[SchemaT]:
        state = self.canonical_row(row_id)
        if state is None:
            state = self.get(row_id)
        return self._create_row(row_id) if state is None else state

    def _create_row(self, row_id: str) -> State[SchemaT]:
        self._validate_row_id(row_id)
        state = self._row_factory(row_id)
        if state.row_id != row_id:
            raise StoreStateError(
                "State row ID does not match collection membership"
            )
        return state

    def _validate_row_id(self, row_id: str) -> None:
        if not row_id:
            raise ValueError("Collection row ID must be a non-empty string")

    def _require_row(self, row_id: str) -> State[SchemaT]:
        state = self.canonical_row(row_id)
        if state is None:
            raise RuntimeError(
                f"Canonical event references an unknown row: {row_id!r}"
            )
        return state

    def on_enter(self, row_id: str, state: State[SchemaT]) -> None:
        pass

    def on_leave(self, row_id: str, state: State[SchemaT]) -> None:
        pass
