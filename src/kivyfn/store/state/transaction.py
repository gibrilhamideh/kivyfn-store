from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kivyfn.store.state.operation import (
    StateOperation,
    StateOperationKind,
    StateResult,
)


if TYPE_CHECKING:
    from kivyfn.store.state.collection import CollectionState
    from kivyfn.store.state.entity import EntityState
    from kivyfn.store.state.manager import BaseStateManager


@dataclass(slots=True)
class StateAction:
    kind: StateOperationKind
    entity: EntityState
    collection: CollectionState | None = None
    values: dict[str, Any] | None = None
    signal: str | None = None
    data: Any = None


class StateTransaction:
    """Capture normal state mutations as one atomic store transaction."""

    def __init__(self, manager: BaseStateManager) -> None:
        self._manager = manager
        self._token: Any = None
        self._actions: list[StateAction] = []
        self._updates: dict[EntityState, StateAction] = {}
        self._inserts: dict[EntityState, StateAction] = {}
        self._removes: dict[EntityState, StateAction] = {}
        self._signal_entities: set[EntityState] = set()
        self._entered = False
        self._submitted = False
        self._error: BaseException | None = None
        self.operation = StateOperation(
            StateOperationKind.TRANSACTION,
            optimistic=True,
        )

    def __enter__(self) -> StateTransaction:
        if self._entered:
            raise RuntimeError(
                "A state transaction cannot be entered twice"
            )

        self._entered = True
        self._manager._attach_operation(self.operation)
        self._token = self._manager._enter_transaction(self)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> bool:
        self._manager._leave_transaction(self._token)
        self._submitted = True
        error = exc or self._error

        if error is not None:
            self._manager._cancel_transaction(self, error)
            return False

        self.operation.requested = tuple(self._actions)
        self._manager._submit_state_transaction(self)
        return False

    def __await__(self):
        return self.operation.__await__()

    def add_update(
        self,
        entity: EntityState,
        values: dict[str, Any],
    ) -> None:
        self._require_active()

        if entity in self._removes:
            raise RuntimeError(
                "A removed state row cannot be updated in the same "
                "transaction"
            )

        inserted = self._inserts.get(entity)

        if inserted is not None:
            if inserted.values is None:
                inserted.values = {}

            inserted.values.update(values)
            return

        action = self._updates.get(entity)

        if action is None:
            action = StateAction(
                kind=StateOperationKind.UPDATE,
                entity=entity,
                values={},
            )
            self._updates[entity] = action
            self._actions.append(action)

        if action.values is None:
            action.values = {}

        action.values.update(values)

    def add_insert(
        self,
        collection: CollectionState,
        entity: EntityState,
        values: dict[str, Any],
    ) -> None:
        self._require_active()

        if entity in self._inserts or entity in self._removes:
            raise RuntimeError(
                "Conflicting insert/remove operations for one state row"
            )

        action = StateAction(
            kind=StateOperationKind.INSERT,
            entity=entity,
            collection=collection,
            values=dict(values),
        )
        self._inserts[entity] = action
        self._actions.append(action)

    def add_remove(
        self,
        collection: CollectionState,
        entity: EntityState,
    ) -> StateOperationKind | None:
        self._require_active()

        if entity in self._signal_entities:
            raise RuntimeError(
                "A state row cannot emit a signal and be removed in the "
                "same transaction"
            )

        inserted = self._inserts.pop(entity, None)

        if inserted is not None:
            self._actions.remove(inserted)
            return StateOperationKind.INSERT

        updated = self._updates.pop(entity, None)

        if updated is not None:
            self._actions.remove(updated)

        if entity in self._removes:
            raise RuntimeError(
                "A state row cannot be removed twice in one transaction"
            )

        action = StateAction(
            kind=StateOperationKind.REMOVE,
            entity=entity,
            collection=collection,
        )
        self._removes[entity] = action
        self._actions.append(action)
        return None

    def add_signal(
        self,
        entity: EntityState,
        signal: str,
        data: Any,
    ) -> None:
        self._require_active()

        if entity in self._removes:
            raise RuntimeError(
                "A removed state row cannot emit a signal in the same "
                "transaction"
            )

        if entity in self._inserts:
            raise RuntimeError(
                "A newly inserted row cannot emit a signal in the same "
                "transaction"
            )

        self._signal_entities.add(entity)
        self._actions.append(
            StateAction(
                kind=StateOperationKind.SIGNAL,
                entity=entity,
                signal=signal,
                data=data,
            )
        )

    def fail(self, error: BaseException) -> None:
        if self._error is None:
            self._error = error

    def _require_active(self) -> None:
        if not self._entered or self._submitted:
            raise RuntimeError(
                "State transaction is not accepting operations"
            )

    @property
    def actions(self) -> tuple[StateAction, ...]:
        return tuple(self._actions)

    @property
    def failed(self) -> bool:
        return self._error is not None

    @property
    def done(self) -> bool:
        return self.operation.done

    @property
    def result(self) -> StateResult | None:
        return self.operation.result
