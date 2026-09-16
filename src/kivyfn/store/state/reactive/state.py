# src/kivyfn/store/state/reactive/state.py
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, Generic, Protocol, TypeVar
from weakref import WeakSet

from kivy.event import EventDispatcher
from kivy.properties import BooleanProperty, NumericProperty
from kivyfn.logging import get_logger

from kivyfn.store.error import StoreStateError
from kivyfn.store.protocol.message.event import EventAction, SetEventAction
from kivyfn.store.schema import Schema
from kivyfn.store.schema.fields import BaseField
from kivyfn.store.state.reactive.base import TransactionStatus
from kivyfn.store.state.reactive.data import StateData
from kivyfn.store.state.reactive.dispatch import notify_fields, set_property

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
    __events__ = TransactionStatus.events

    _state_field_names: frozenset[str] = frozenset()
    _state_field_metadata: Mapping[str, BaseField[Any]] = {}

    def __init__(
        self,
        manager: StateManagerProtocol,
        schema: SchemaT,
        row_id: str | None,
    ) -> None:
        super().__init__()

        self._transaction_status = TransactionStatus()
        self._manager = manager
        self._schema = schema
        self._row_id = row_id
        self._data = StateData(schema.defaults())
        self._synced_owners: WeakSet[object] = WeakSet()

        self.publish(self._state_field_names)

    def __getattribute__(self, name: str) -> Any:
        if name in {"pending_count", "is_pending"}:
            try:
                status = object.__getattribute__(self, "_transaction_status")
            except AttributeError:
                pass
            else:
                return len(status) if name == "pending_count" else bool(status)
        # Properties notify Kivy; StateData is the authoritative read view.
        # Copying here also prevents in-place edits escaping request routing.
        if name == "synced":
            try:
                row_id = object.__getattribute__(self, "_row_id")
                owners = object.__getattribute__(self, "_synced_owners")
            except AttributeError:
                pass
            else:
                if row_id is not None:
                    return bool(owners)
        if not name.startswith("_"):
            fields = type(self)._state_field_names
            if name in fields:
                try:
                    data = object.__getattribute__(self, "_data")
                except AttributeError:
                    pass
                else:
                    return data.visible(name)
        return super().__getattribute__(name)

    def set_synced(self, synced: bool) -> None:
        set_property(self, "synced", synced)

    def set_owner_synced(self, owner: object, synced: bool) -> None:
        """Stay synced while any owning collection keeps this row current."""
        if synced:
            self._synced_owners.add(owner)
        else:
            self._synced_owners.discard(owner)
        set_property(self, "synced", bool(self._synced_owners))

    def get(self, name: str) -> Any:
        """Read a named field, including dynamically projected query fields."""
        self._require_fields((name,))
        return self._data.visible(name)

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
        """Record a write without changing the visible value optimistically.

        Joins the active transaction, or submits a new one immediately.
        Await the returned transaction to observe completion.
        """
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

    def set_canonical(self, values: Mapping[str, Any]) -> None:
        """Install host values and notify visible changes after the batch."""
        self._require_fields(values)
        notify_fields(self, self._data.set_canonical(values))

    def set_overlay(
        self, transaction_id: str, values: Mapping[str, Any]
    ) -> None:
        """Apply this transaction's optimistic values and own notification."""
        self._require_fields(values)
        notify_fields(self, self._data.set_overlay(transaction_id, values))

    def discard_overlay(
        self, transaction_id: str, keys: Iterable[str] | None = None
    ) -> None:
        """Remove only this transaction's contribution, then notify changes."""
        notify_fields(self, self._data.discard_overlay(transaction_id, keys))

    def apply_event(self, action: EventAction) -> None:
        """Apply singleton host values; collections handle row membership."""
        if not isinstance(action, SetEventAction):
            raise TypeError("State target requires a Set event")
        self.set_canonical(dict(action.values))

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
        self._transaction_status.begin(self, transaction)

    def succeed_transaction(self, transaction: StateTransaction) -> None:
        self._transaction_status.succeed(self, transaction)

    def fail_transaction(
        self, transaction: StateTransaction, failure: TransactionFailure
    ) -> None:
        self._transaction_status.fail(self, transaction, failure)

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

    def _require_fields(self, values: Iterable[str]) -> None:
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
