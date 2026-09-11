# src/kivyfn/store/state/reactive/state.py
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Generic, Protocol, TypeVar

from kivy.event import EventDispatcher
from kivy.properties import BooleanProperty, NumericProperty

from kivyfn.logging import get_logger
from kivyfn.store.error import StoreStateError
from kivyfn.store.schema import Schema
from kivyfn.store.schema.fields import BaseField
from kivyfn.store.state.mutation.failure import StateFailure
from kivyfn.store.state.mutation.operation import StateOperation
from kivyfn.store.state.reactive.data import StateData


logger = get_logger(__name__)

SchemaT = TypeVar("SchemaT", bound=Schema)


class StateManagerProtocol(Protocol):
    def request_set(
        self, state: State[Any], values: dict[str, Any], *, optimistic: bool
    ) -> StateOperation: ...

    def request_setrow(
        self, state: State[Any], values: dict[str, Any], *, optimistic: bool
    ) -> StateOperation: ...

    def request_signal(
        self, state: State[Any], signal: str, data: Any
    ) -> StateOperation: ...


class State(EventDispatcher, Generic[SchemaT]):
    """Reactive representation of one Store record."""

    synced = BooleanProperty(False)
    pending = BooleanProperty(False)
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
        self._pending_operations: set[str] = set()

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

    def set(self, **values: Any) -> StateOperation:
        """Start one strict field set."""
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

    def stage_canonical(self, values: Mapping[str, Any]) -> frozenset[str]:
        self._require_fields(values)

        return self._data.set_canonical(values)

    def stage_overlay(
        self, operation: StateOperation, values: Mapping[str, Any]
    ) -> frozenset[str]:
        self._require_fields(values)

        return self._data.set_overlay(operation.id, values)

    def discard_overlay(
        self,
        operation: StateOperation,
        fields: set[str] | frozenset[str] | None = None,
    ) -> frozenset[str]:
        return self._data.remove_overlay(operation.id, fields)

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

    def begin_operation(
        self, operation: StateOperation, key: str | None, old: Any, new: Any
    ) -> None:
        if operation.id not in self._pending_operations:
            self._pending_operations.add(operation.id)
            self._sync_pending()

        self.dispatch("on_pending", operation, key, old, new)

    def succeed_operation(
        self, operation: StateOperation, key: str | None, old: Any, new: Any
    ) -> None:
        self._finish_operation(operation)

        self.dispatch("on_success", operation, key, old, new)
        self.dispatch("on_done", operation, key, old, new, None)

    def fail_operation(
        self,
        operation: StateOperation,
        key: str | None,
        old: Any,
        new: Any,
        failure: StateFailure,
    ) -> None:
        self._finish_operation(operation)

        self.dispatch("on_failed", operation, key, old, new, failure)
        self.dispatch("on_done", operation, key, old, new, failure)

    def _request_set(
        self, values: dict[str, Any], *, optimistic: bool
    ) -> StateOperation:
        if self._row_id is None:
            return self._manager.request_set(
                self, values, optimistic=optimistic
            )

        return self._manager.request_setrow(
            self, values, optimistic=optimistic
        )

    def _emit_signal(self, signal: str, data: Any = None) -> StateOperation:
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

    def _finish_operation(self, operation: StateOperation) -> None:
        self._pending_operations.discard(operation.id)
        self._sync_pending()

    def _sync_pending(self) -> None:
        count = len(self._pending_operations)

        self.pending_count = count
        self.pending = count > 0

    def _require_fields(self, values: Mapping[str, Any]) -> None:
        for name in values:
            if name not in self._state_field_names:
                raise StoreStateError(
                    f"Unknown State field {name!r} for {self._schema.name}"
                )

    def on_pending(
        self, operation: StateOperation, key: str | None, old: Any, new: Any
    ) -> None:
        pass

    def on_success(
        self, operation: StateOperation, key: str | None, old: Any, new: Any
    ) -> None:
        pass

    def on_failed(
        self,
        operation: StateOperation,
        key: str | None,
        old: Any,
        new: Any,
        failure: StateFailure,
    ) -> None:
        pass

    def on_done(
        self,
        operation: StateOperation,
        key: str | None,
        old: Any,
        new: Any,
        failure: StateFailure | None,
    ) -> None:
        pass

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"schema={self._schema.name!r}, "
            f"row_id={self._row_id!r}"
            ")"
        )
