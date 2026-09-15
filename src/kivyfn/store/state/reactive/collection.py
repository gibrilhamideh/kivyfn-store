# src/kivyfn/store/state/reactive/collection.py
from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Generic, Protocol, TypeVar

from kivy.event import EventDispatcher
from kivy.properties import AliasProperty, BooleanProperty, NumericProperty
from kivyfn.logging import get_logger

from kivyfn.store.error import StoreStateError
from kivyfn.store.schema import ManySchema
from kivyfn.store.state.reactive.data import StateData
from kivyfn.store.state.reactive.dispatch import dispatch, set_property
from kivyfn.store.state.reactive.state import State

if TYPE_CHECKING:
    from kivyfn.store.state.transaction.failure import TransactionFailure
    from kivyfn.store.state.transaction.transaction import StateTransaction


logger = get_logger(__name__)

SchemaT = TypeVar("SchemaT", bound=ManySchema)

RowFactory = Callable[[str], State[SchemaT]]


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


@dataclass(frozen=True, slots=True)
class MembershipDelta(Generic[SchemaT]):
    """Visible membership changes for one collection."""

    entered: tuple[tuple[str, State[SchemaT]], ...] = ()
    left: tuple[tuple[str, State[SchemaT]], ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.entered or self.left)


class CollectionState(EventDispatcher, Generic[SchemaT]):
    """Editable reactive keyed collection of State objects."""

    _row_version = NumericProperty(0)
    synced = BooleanProperty(False)
    is_pending = BooleanProperty(False)
    pending_count = NumericProperty(0)

    __events__ = (
        "on_pending",
        "on_success",
        "on_failed",
        "on_done",
        "on_insert",
        "on_remove",
        "on_enter",
        "on_leave",
    )

    def __init__(
        self,
        manager: CollectionManagerProtocol,
        schema: SchemaT,
        row_factory: RowFactory[SchemaT],
    ) -> None:
        super().__init__()

        self._manager = manager
        self._schema = schema
        self._row_factory = row_factory

        self._canonical_rows: dict[str, State[SchemaT]] = {}
        self._membership_overlays: dict[str, dict[str, bool]] = {}
        self._rows: dict[str, State[SchemaT]] = {}
        self._rows_view: Mapping[str, State[SchemaT]] = MappingProxyType(
            self._rows
        )

        self._pending_transactions: set[str] = set()

    def __getattribute__(self, name: str) -> Any:
        if name in {"pending_count", "is_pending"}:
            try:
                pending = object.__getattribute__(
                    self, "_pending_transactions"
                )
            except AttributeError:
                pass
            else:
                return (
                    len(pending) if name == "pending_count" else bool(pending)
                )
        return super().__getattribute__(name)

    @property
    def schema(self) -> SchemaT:
        return self._schema

    def _get_rows(self) -> Mapping[str, State[SchemaT]]:
        return self._rows_view

    rows = AliasProperty(_get_rows, None, bind=("_row_version",))

    def __getitem__(self, row_id: str) -> State[SchemaT]:
        return self._rows[row_id]

    def __setitem__(self, row_id: str, values: Mapping[str, Any]) -> None:
        if row_id in self._rows:
            raise ValueError(f"Collection row already exists: {row_id!r}")

        self._manager.request_insert(
            self, row_id, self._prepare_values(values), optimistic=True
        )

    def __delitem__(self, row_id: str) -> None:
        if row_id not in self._rows:
            raise KeyError(row_id)

        self._manager.request_remove(self, row_id, optimistic=True)

    def __iter__(self) -> Iterator[str]:
        return iter(self._rows)

    def __len__(self) -> int:
        return len(self._rows)

    def __contains__(self, row_id: object) -> bool:
        return row_id in self._rows

    def get(self, row_id: str) -> State[SchemaT] | None:
        return self._rows.get(row_id)

    def keys(self) -> tuple[str, ...]:
        return tuple(self._rows)

    def values(self) -> tuple[State[SchemaT], ...]:
        return tuple(self._rows.values())

    def items(self) -> tuple[tuple[str, State[SchemaT]], ...]:
        return tuple(self._rows.items())

    def insert(self, row_id: str, **values: Any) -> StateTransaction:
        if row_id in self._rows:
            raise ValueError(f"Collection row already exists: {row_id!r}")

        return self._manager.request_insert(
            self, row_id, self._prepare_values(values), optimistic=False
        )

    def remove(self, row_id: str) -> StateTransaction:
        if row_id not in self._rows:
            raise KeyError(row_id)

        return self._manager.request_remove(self, row_id, optimistic=False)

    def canonical_row(self, row_id: str) -> State[SchemaT] | None:
        """Find a canonical row even when an optimistic remove hides it."""
        return self._canonical_rows.get(row_id)

    def sync_rows(self, synced: bool) -> None:
        for state in self._canonical_rows.values():
            state.set_owner_synced(self, synced)
        set_property(self, "synced", synced)

    def enter(self, row_id: str) -> MembershipDelta[SchemaT]:
        if row_id not in self._canonical_rows:
            self._canonical_rows[row_id] = self._create_row(row_id)
        return self._refresh_membership()

    def leave(self, row_id: str) -> MembershipDelta[SchemaT]:
        state = self._canonical_rows.pop(row_id, None)
        if state is not None:
            state.set_owner_synced(self, False)
        return self._refresh_membership()

    def set_membership(
        self, row_ids: Iterable[str]
    ) -> MembershipDelta[SchemaT]:
        row_ids = tuple(row_ids)
        target_ids = set(row_ids)
        for row_id in tuple(self._canonical_rows):
            if row_id not in target_ids:
                state = self._canonical_rows.pop(row_id)
                state.set_owner_synced(self, False)
        for row_id in row_ids:
            if row_id not in self._canonical_rows:
                self._canonical_rows[row_id] = self._create_row(row_id)
        return self._refresh_membership()

    def set_membership_overlay(
        self, transaction_id: str, row_id: str, present: bool
    ) -> MembershipDelta[SchemaT]:
        overlay = self._membership_overlays.setdefault(transaction_id, {})
        overlay[row_id] = present
        return self._refresh_membership()

    def discard_membership_overlay(
        self, transaction_id: str, row_id: str | None = None
    ) -> MembershipDelta[SchemaT]:
        overlay = self._membership_overlays.get(transaction_id)
        if overlay is not None:
            if row_id is None:
                del self._membership_overlays[transaction_id]
            else:
                overlay.pop(row_id, None)
                if not overlay:
                    del self._membership_overlays[transaction_id]
        return self._refresh_membership()

    def _refresh_membership(self) -> MembershipDelta[SchemaT]:
        rows = dict(self._canonical_rows)
        for overlay in self._membership_overlays.values():
            for row_id, present in overlay.items():
                if present:
                    state = rows.get(row_id)
                    rows[row_id] = (
                        state
                        or self._rows.get(row_id)
                        or self._create_row(row_id)
                    )
                else:
                    rows.pop(row_id, None)
        delta = MembershipDelta(
            entered=tuple(
                (key, row)
                for key, row in rows.items()
                if key not in self._rows
            ),
            left=tuple(
                (key, row)
                for key, row in self._rows.items()
                if key not in rows
            ),
        )
        self._rows.clear()
        self._rows.update(rows)
        for state in self._canonical_rows.values():
            state.set_owner_synced(self, bool(self.synced))
        return delta

    def publish_membership(self, delta: MembershipDelta[SchemaT]) -> None:
        if not delta.changed:
            return

        set_property(self, "_row_version", self._row_version + 1)

        for row_id, state in delta.left:
            self._dispatch_membership("on_leave", row_id, state)

        for row_id, state in delta.entered:
            self._dispatch_membership("on_enter", row_id, state)

    def begin_transaction(self, transaction: StateTransaction) -> None:
        if transaction.id in self._pending_transactions:
            return

        self._pending_transactions.add(transaction.id)
        self._sync_pending()

        dispatch(self, "on_pending", transaction)

    def succeed_transaction(self, transaction: StateTransaction) -> None:
        if not self._finish_transaction(transaction.id):
            return

        dispatch(self, "on_success", transaction)
        dispatch(self, "on_done", transaction, None)

    def fail_transaction(
        self, transaction: StateTransaction, failure: TransactionFailure
    ) -> None:
        if not self._finish_transaction(transaction.id):
            return

        dispatch(self, "on_failed", transaction, failure)
        dispatch(self, "on_done", transaction, failure)

    def publish_insert(self, row_id: str, state: State[SchemaT]) -> None:
        dispatch(self, "on_insert", row_id, state)

    def publish_remove(self, row_id: str, state: State[SchemaT]) -> None:
        dispatch(self, "on_remove", row_id, state)

    def publish_signal(
        self, row_id: str, signal: str, data: Any = None
    ) -> None:
        dispatch(self, f"on_{signal}", row_id, StateData.copy(data))

    def _prepare_values(self, values: Mapping[str, Any]) -> dict[str, Any]:
        copied = StateData.copy_values(values)

        for key, value in copied.items():
            self._schema.challenge_field(key, value)

        return copied

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

    def _dispatch_membership(
        self, event: str, row_id: str, state: State[SchemaT]
    ) -> None:
        try:
            self.dispatch(event, row_id, state)
        except Exception:
            logger.exception(
                "Collection membership callback failed: "
                "schema=%s event=%s row_id=%s",
                self._schema.name,
                event,
                row_id,
            )

    def _finish_transaction(self, transaction_id: str) -> bool:
        if transaction_id not in self._pending_transactions:
            return False

        self._pending_transactions.remove(transaction_id)
        self._sync_pending()

        return True

    def _sync_pending(self) -> None:
        count = len(self._pending_transactions)

        set_property(self, "pending_count", count)
        set_property(self, "is_pending", count > 0)

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

    def on_enter(self, row_id: str, state: State[SchemaT]) -> None:
        pass

    def on_leave(self, row_id: str, state: State[SchemaT]) -> None:
        pass

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"schema={self._schema.name!r}, "
            f"rows={len(self._rows)}"
            ")"
        )
