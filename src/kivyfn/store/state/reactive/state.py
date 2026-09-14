# src/kivyfn/store/state/reactive/state.py
from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Generic, Protocol, TypeVar

from kivy.event import EventDispatcher
from kivy.properties import BooleanProperty, NumericProperty

from kivyfn.logging import get_logger
from kivyfn.store.error import StoreStateError
from kivyfn.store.schema import Schema
from kivyfn.store.schema.fields import BaseField
from kivyfn.store.state.reactive.data import StateData

if TYPE_CHECKING:
    from kivyfn.store.state.transaction.failure import TransactionFailure
    from kivyfn.store.state.transaction.transaction import StateTransaction


logger = get_logger(__name__)

SchemaT = TypeVar("SchemaT", bound=Schema)


class StateManagerProtocol(Protocol):
    def request_set(
        self, state: State[Any], values: dict[str, Any], *, optimistic: bool
    ) -> StateTransaction: ...

    def request_setrow(
        self, state: State[Any], values: dict[str, Any], *, optimistic: bool
    ) -> StateTransaction: ...

    def request_signal(
        self, state: State[Any], signal: str, data: Any
    ) -> StateTransaction: ...


class State(EventDispatcher, Generic[SchemaT]):
    """Reactive representation of one Store record."""

    synced = BooleanProperty(False)
    is_pending = BooleanProperty(False)
    pending_count = NumericProperty(0)

    __events__ = ("on_pending", "on_success", "on_failed", "on_done")

    _state_field_names: frozenset[str] = frozenset()
    _state_field_metadata: Mapping[str, BaseField[Any]] = {}

    def __init__(
        self,
        manager: StateManagerProtocol,
        schema: SchemaT,
        row_id: str | None,
    ) -> None:
        super().__init__()

        self._manager = manager
        self._schema = schema
        self._row_id = row_id
        self._data = StateData(schema.defaults())
        self._pending_transactions: set[str] = set()

        self.publish(self._state_field_names)

    def __setattr__(self, name: str, value: Any) -> None:
        if not name.startswith("_") and name in type(self)._state_field_names:
            try:
                data = object.__getattribute__(self, "_data")
            except AttributeError:
                data = None

            if data is not None:
                current = data.visible(name)

                if current == value:
                    return

                copied = data.copy(value)
                self.validate_values({name: copied})
                self._request_set({name: copied}, optimistic=True)
                return

        super().__setattr__(name, value)

    @property
    def schema(self) -> SchemaT:
        return self._schema

    @property
    def fields(self) -> Mapping[str, BaseField[Any]]:
        return type(self)._state_field_metadata

    @property
    def row_id(self) -> str | None:
        return self._row_id

    def set(self, **values: Any) -> StateTransaction:
        """Start one non-optimistic State transaction."""
        if not values:
            raise ValueError("set() requires at least one field")

        copied = self._data.copy_values(values)
        self.validate_values(copied)

        return self._request_set(copied, optimistic=False)

    def validate_values(self, values: Mapping[str, Any]) -> None:
        self._require_fields(values)

        fields = type(self)._state_field_metadata

        for key, value in values.items():
            fields[key].challenge(value)

    def set_canonical(self, values: Mapping[str, Any]) -> frozenset[str]:
        self._require_fields(values)
        return self._data.set_canonical(values)

    def set_overlay(
        self, transaction_id: str, values: Mapping[str, Any]
    ) -> frozenset[str]:
        self._require_fields(values)
        return self._data.set_overlay(transaction_id, values)

    def discard_overlay(
        self,
        transaction_id: str,
        fields: set[str] | frozenset[str] | None = None,
    ) -> frozenset[str]:
        return self._data.remove_overlay(transaction_id, fields)

    def publish(self, fields: set[str] | frozenset[str]) -> None:
        for key in fields:
            self._publish_field(key)

    def visible_snapshot(
        self, fields: set[str] | frozenset[str]
    ) -> dict[str, Any]:
        return self._data.visible_snapshot(fields)

    def canonical_snapshot(
        self, fields: set[str] | frozenset[str]
    ) -> dict[str, Any]:
        return self._data.canonical_snapshot(fields)

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

    def _request_set(
        self, values: dict[str, Any], *, optimistic: bool
    ) -> StateTransaction:
        if self._row_id is None:
            return self._manager.request_set(
                self, values, optimistic=optimistic
            )

        return self._manager.request_setrow(
            self, values, optimistic=optimistic
        )

    def _emit_signal(self, signal: str, data: Any = None) -> StateTransaction:
        return self._manager.request_signal(
            self, signal, self._data.copy(data)
        )

    def _publish_field(self, key: str) -> None:
        prop = self.property(key)
        value = self._data.visible(key)

        if prop.get(self) == value:
            return

        try:
            prop.set(self, value)
        except Exception:
            logger.exception(
                "State property dispatch failed: schema=%s row_id=%s field=%s",
                self._schema.name,
                self._row_id,
                key,
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

    def _require_fields(self, values: Mapping[str, Any]) -> None:
        for name in values:
            if name not in self._state_field_names:
                raise StoreStateError(
                    f"Unknown State field {name!r} for {self._schema.name}"
                )

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

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"schema={self._schema.name!r}, "
            f"row_id={self._row_id!r}"
            ")"
        )
