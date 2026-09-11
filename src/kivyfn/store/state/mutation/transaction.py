# src/kivyfn/store/state/mutation/transaction.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, TypeAlias

from kivyfn.store.state.mutation.failure import StateFailure
from kivyfn.store.state.mutation.operation import StateOperation
from kivyfn.store.state.reactive.collection import CollectionState
from kivyfn.store.state.reactive.data import StateData
from kivyfn.store.state.reactive.state import State


@dataclass(slots=True)
class SetAction:
    """Field updates for one SingleSchema State."""

    state: State[Any]
    old: dict[str, Any]
    new: dict[str, Any]


@dataclass(slots=True)
class SetRowAction:
    """Field updates for one existing ManySchema row State."""

    state: State[Any]
    old: dict[str, Any]
    new: dict[str, Any]


@dataclass(slots=True)
class InsertAction:
    """Insertion of one CollectionState row."""

    collection: CollectionState[Any]
    state: State[Any]
    new: dict[str, Any]


@dataclass(slots=True)
class RemoveAction:
    """Removal of one CollectionState row."""

    collection: CollectionState[Any]
    state: State[Any]
    old: dict[str, Any]


@dataclass(slots=True)
class SignalAction:
    """One State signal invocation."""

    state: State[Any]
    signal: str
    data: Any


StateAction: TypeAlias = (
    SetAction | SetRowAction | InsertAction | RemoveAction | SignalAction
)


class _TransactionManager(Protocol):
    def enter_transaction(self, transaction: StateTransaction) -> object: ...

    def leave_transaction(self, token: object) -> None: ...

    def request_transaction(self, transaction: StateTransaction) -> None: ...

    def abort_transaction(
        self, transaction: StateTransaction, error: BaseException
    ) -> None: ...


class StateTransaction:
    """Capture State mutations as one atomic optimistic operation."""

    def __init__(self, manager: _TransactionManager) -> None:
        self._manager = manager
        self._token: object | None = None
        self._entered = False
        self._closed = False

        self._actions: list[StateAction] = []

        self._sets: dict[State[Any], SetAction] = {}
        self._setrows: dict[State[Any], SetRowAction] = {}
        self._inserts: dict[State[Any], InsertAction] = {}
        self._removes: dict[State[Any], RemoveAction] = {}

        self._signal_states: set[State[Any]] = set()

        self.operation = StateOperation("transaction", optimistic=True)

    def __enter__(self) -> StateTransaction:
        if self._entered:
            raise RuntimeError("A State transaction cannot be entered twice")

        self._entered = True
        self._token = self._manager.enter_transaction(self)

        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> bool:
        del exc_type
        del traceback

        token = self._token

        if token is None:
            raise RuntimeError("State transaction has no active context token")

        self._manager.leave_transaction(token)

        self._token = None
        self._closed = True

        if exc is not None:
            self._manager.abort_transaction(self, exc)
            return False

        if not self._actions:
            self.operation.succeed()
            return False

        try:
            self._manager.request_transaction(self)
        except BaseException as error:
            self._manager.abort_transaction(self, error)
            raise

        return False

    def __await__(self):
        return self.operation.__await__()

    @property
    def actions(self) -> tuple[StateAction, ...]:
        return tuple(self._actions)

    @property
    def done(self) -> bool:
        return self.operation.done

    @property
    def success(self) -> bool:
        return self.operation.success

    @property
    def failed(self) -> bool:
        return self.operation.failed

    @property
    def failure(self) -> StateFailure | None:
        return self.operation.failure

    def add_set(
        self, state: State[Any], old: dict[str, Any], new: dict[str, Any]
    ) -> None:
        self._require_active()

        if state.row_id is not None:
            raise ValueError("SetAction requires a State without a row ID")

        action = self._sets.get(state)

        if action is None:
            action = SetAction(
                state=state,
                old=StateData.copy_values(old),
                new=StateData.copy_values(new),
            )
            self._sets[state] = action
            self._actions.append(action)
            return

        _merge_set(action.old, action.new, old, new)

    def add_setrow(
        self, state: State[Any], old: dict[str, Any], new: dict[str, Any]
    ) -> None:
        self._require_active()

        if state.row_id is None:
            raise ValueError("SetRowAction requires a State row ID")

        inserted = self._inserts.get(state)

        if inserted is not None:
            inserted.new.update(StateData.copy_values(new))
            return

        if state in self._removes:
            raise RuntimeError(
                "A removed State row cannot be updated in one transaction"
            )

        action = self._setrows.get(state)

        if action is None:
            action = SetRowAction(
                state=state,
                old=StateData.copy_values(old),
                new=StateData.copy_values(new),
            )
            self._setrows[state] = action
            self._actions.append(action)
            return

        _merge_set(action.old, action.new, old, new)

    def add_insert(
        self,
        collection: CollectionState[Any],
        state: State[Any],
        new: dict[str, Any],
    ) -> None:
        self._require_active()

        if state.row_id is None:
            raise ValueError("InsertAction requires a State row ID")

        if state in self._inserts:
            raise RuntimeError(
                "A State row cannot be inserted twice in one transaction"
            )

        if state in self._setrows and state not in self._removes:
            raise RuntimeError(
                "An existing State row must be removed before insertion"
            )

        action = InsertAction(
            collection=collection, state=state, new=StateData.copy_values(new)
        )

        self._inserts[state] = action
        self._actions.append(action)

    def add_remove(
        self,
        collection: CollectionState[Any],
        state: State[Any],
        old: dict[str, Any],
    ) -> bool:
        """
        Capture one remove.

        True means a prior insert was cancelled by this remove.
        """
        self._require_active()

        if state.row_id is None:
            raise ValueError("RemoveAction requires a State row ID")

        if state in self._signal_states:
            raise RuntimeError(
                "A signalled State row cannot be removed "
                "in the same transaction"
            )

        inserted = self._inserts.pop(state, None)

        if inserted is not None:
            self._actions.remove(inserted)
            return True

        if state in self._removes:
            raise RuntimeError(
                "A State row cannot be removed twice in one transaction"
            )

        original_old = StateData.copy_values(old)

        updated = self._setrows.pop(state, None)

        if updated is not None:
            self._actions.remove(updated)
            original_old = StateData.copy_values(updated.old)

        action = RemoveAction(
            collection=collection, state=state, old=original_old
        )

        self._removes[state] = action
        self._actions.append(action)

        return False

    def add_signal(self, state: State[Any], signal: str, data: Any) -> None:
        self._require_active()

        if not isinstance(signal, str) or not signal:
            raise ValueError("State signal name must be a non-empty string")

        if state in self._inserts:
            raise RuntimeError(
                "A newly inserted State row cannot emit a signal "
                "in the same transaction"
            )

        if state in self._removes:
            raise RuntimeError(
                "A removed State row cannot emit a signal "
                "in the same transaction"
            )

        self._signal_states.add(state)

        self._actions.append(
            SignalAction(state=state, signal=signal, data=StateData.copy(data))
        )

    def _require_active(self) -> None:
        if not self._entered or self._closed:
            raise RuntimeError("State transaction is not accepting mutations")


def _merge_set(
    target_old: dict[str, Any],
    target_new: dict[str, Any],
    old: dict[str, Any],
    new: dict[str, Any],
) -> None:
    for key, value in old.items():
        target_old.setdefault(key, StateData.copy(value))

    target_new.update(StateData.copy_values(new))
