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
    """Membership changes for one CollectionState."""

    entered: tuple[tuple[str, State[SchemaT]], ...] = ()
    left: tuple[tuple[str, State[SchemaT]], ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.entered or self.left)


class CollectionState(EventDispatcher, Generic[SchemaT]):
    """Editable reactive keyed collection of State objects."""

    synced = BooleanProperty(False)
    is_pending = BooleanProperty(False)
    pending_count = NumericProperty(0)
    _rows_version = NumericProperty(0)

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

        self._rows: dict[str, State[SchemaT]] = {}
        self._rows_view: Mapping[str, State[SchemaT]] = MappingProxyType(
            self._rows
        )

        self._pending_transactions: set[str] = set()

    @property
    def schema(self) -> SchemaT:
        return self._schema

    def _get_rows(self) -> Mapping[str, State[SchemaT]]:
        return self._rows_view

    rows = AliasProperty(_get_rows, None, bind=("_rows_version",), cache=True)

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

    def enter(self, row_id: str) -> MembershipDelta[SchemaT]:
        self._validate_row_id(row_id)

        existing = self._rows.get(row_id)

        if existing is not None:
            return MembershipDelta()

        state = self._create_row(row_id)
        self._rows[row_id] = state

        return MembershipDelta(entered=((row_id, state),))

    def leave(self, row_id: str) -> MembershipDelta[SchemaT]:
        state = self._rows.pop(row_id, None)

        if state is None:
            return MembershipDelta()

        return MembershipDelta(left=((row_id, state),))

    def set_membership(
        self, row_ids: Iterable[str]
    ) -> MembershipDelta[SchemaT]:
        ordered_ids = tuple(row_ids)

        for row_id in ordered_ids:
            self._validate_row_id(row_id)

        if len(ordered_ids) != len(set(ordered_ids)):
            raise ValueError(
                "Collection membership cannot contain duplicate row IDs"
            )

        target_ids = set(ordered_ids)

        entered: list[tuple[str, State[SchemaT]]] = []
        left: list[tuple[str, State[SchemaT]]] = []

        for row_id in tuple(self._rows):
            if row_id in target_ids:
                continue

            state = self._rows.pop(row_id)
            left.append((row_id, state))

        for row_id in ordered_ids:
            if row_id in self._rows:
                continue

            state = self._create_row(row_id)
            self._rows[row_id] = state

            entered.append((row_id, state))

        return MembershipDelta(entered=tuple(entered), left=tuple(left))

    def publish_membership(self, delta: MembershipDelta[SchemaT]) -> None:
        if not delta.changed:
            return

        self._rows_version += 1

        for row_id, state in delta.left:
            self._dispatch_membership("on_leave", row_id, state)

        for row_id, state in delta.entered:
            self._dispatch_membership("on_enter", row_id, state)

    def begin_transaction(self, transaction: StateTransaction) -> None:
        if transaction.id in self._pending_transactions:
            return

        self._pending_transactions.add(transaction.id)
        self._sync_pending()

        self.dispatch("on_pending", transaction)

    def succeed_transaction(self, transaction: StateTransaction) -> None:
        if not self._finish_transaction(transaction.id):
            return

        self.dispatch("on_success", transaction)
        self.dispatch("on_done", transaction, None)

    def fail_transaction(
        self, transaction: StateTransaction, failure: TransactionFailure
    ) -> None:
        if not self._finish_transaction(transaction.id):
            return

        self.dispatch("on_failed", transaction, failure)
        self.dispatch("on_done", transaction, failure)

    def publish_insert(self, row_id: str, state: State[SchemaT]) -> None:
        self.dispatch("on_insert", row_id, state)

    def publish_remove(self, row_id: str, state: State[SchemaT]) -> None:
        self.dispatch("on_remove", row_id, state)

    def _prepare_values(self, values: Mapping[str, Any]) -> dict[str, Any]:
        if not values:
            raise ValueError("Collection mutation requires at least one field")

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

        self.pending_count = count
        self.is_pending = count > 0

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
