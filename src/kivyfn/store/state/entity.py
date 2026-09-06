from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from functools import partial
from typing import (
    TYPE_CHECKING,
    Any,
    Generic,
    TypeVar,
    cast,
)
from weakref import ref

from kivy.properties import ObjectProperty

from kivyfn.logging import get_logger
from kivyfn.store.error import StoreStateError
from kivyfn.store.state.base import StateEvents
from kivyfn.store.state.operation import StateOperation


if TYPE_CHECKING:
    from kivyfn.store.address import Address
    from kivyfn.store.schema import Schema
    from kivyfn.store.schema.fields import BaseField
    from kivyfn.store.state.manager import BaseStateManager


logger = get_logger(__name__)


SchemaT = TypeVar(
    "SchemaT",
    bound="Schema",
)


_RESERVED_NAMES = {
    "address",
    "bind",
    "dispatch",
    "fbind",
    "failed",
    "fields",
    "funbind",
    "manager",
    "on_failed",
    "on_success",
    "pending",
    "property",
    "row_id",
    "schema",
    "set",
    "setter",
    "success",
    "synced",
    "unbind",
}


_ENTITY_TYPES: dict[
    type[Schema],
    type[EntityState[Any]],
] = {}


class EntityState(
    StateEvents,
    Generic[SchemaT],
):
    """Stable Kivy representation of one canonical schema entity."""

    def __init__(
        self,
        manager: BaseStateManager,
        schema: SchemaT,
        row_id: str | None,
        **kwargs: Any,
    ) -> None:
        self._manager_ref = ref(manager)
        self._schema = schema
        self._row_id = row_id
        self._address: Address[Any] | None = None
        self._applying = True
        self._canonical: dict[str, Any] = {}
        self._visible: dict[str, Any] = {}

        self._overlays: dict[
            str,
            list[tuple[str, Any]],
        ] = defaultdict(list)

        super().__init__(**kwargs)

        for name, value in schema.defaults().items():
            copied = deepcopy(value)

            self._canonical[name] = copied
            self._visible[name] = deepcopy(copied)

            setattr(
                self,
                name,
                deepcopy(copied),
            )

        self._applying = False

        for field_name in schema.field_names():
            self.fbind(
                field_name,
                partial(
                    self._on_field_changed,
                    field_name,
                ),
            )

    def set(
        self,
        **values: Any,
    ) -> StateOperation:
        """Start a confirmed field update and return its operation."""
        if not values:
            raise ValueError(
                "set() requires at least one field"
            )

        return self._manager()._request_update(
            self,
            values,
            optimistic=False,
        )

    def _emit_signal(
        self,
        signal: str,
        data: Any = None,
    ) -> StateOperation:
        return self._manager()._request_signal(
            self,
            signal,
            data,
        )

    def _on_field_changed(
        self,
        field_name: str,
        _instance: EntityState[Any],
        value: Any,
    ) -> None:
        if self._applying:
            return

        previous = deepcopy(
            self._visible.get(field_name)
        )

        if previous == value:
            return

        self._manager()._request_update(
            self,
            {
                field_name: value,
            },
            optimistic=True,
            previous={
                field_name: previous,
            },
        )

    def _hydrate(
        self,
        values: dict[str, Any],
    ) -> None:
        for field_name in self._schema.field_names():
            if field_name not in values:
                continue

            self._canonical[field_name] = deepcopy(
                values[field_name]
            )

            self._refresh_field(
                field_name,
            )

    def _set_relation(
        self,
        relation: str,
        value: EntityState[Any] | None,
    ) -> None:
        if self.property(
            relation,
            quiet=True,
        ) is None:
            return

        self._set_visible(
            relation,
            value,
        )

    def _begin_overlay(
        self,
        operation: StateOperation,
        values: dict[str, Any],
    ) -> None:
        for field_name, value in values.items():
            overlays = self._overlays[field_name]
            copied = deepcopy(value)

            for index, (
                operation_id,
                _current,
            ) in enumerate(overlays):
                if operation_id != operation.id:
                    continue

                overlays[index] = (
                    operation.id,
                    copied,
                )

                break
            else:
                overlays.append(
                    (
                        operation.id,
                        copied,
                    )
                )

            self._refresh_field(
                field_name,
            )

    def _discard_overlay(
        self,
        operation: StateOperation,
        fields: set[str] | None = None,
    ) -> None:
        selected = fields or set(
            self._overlays
        )

        for field_name in tuple(selected):
            overlays = self._overlays.get(
                field_name
            )

            if not overlays:
                continue

            remaining = [
                item
                for item in overlays
                if item[0] != operation.id
            ]

            if remaining:
                self._overlays[field_name] = remaining
            else:
                self._overlays.pop(
                    field_name,
                    None,
                )

            self._refresh_field(
                field_name,
            )

    def _resolve_overlay(
        self,
        operation: StateOperation,
        *,
        accepted: bool,
        values: dict[str, Any] | None = None,
    ) -> None:
        requested = values or {}
        affected = set(requested)

        for field_name, overlays in self._overlays.items():
            if any(
                item[0] == operation.id
                for item in overlays
            ):
                affected.add(
                    field_name
                )

        if accepted:
            for field_name, value in requested.items():
                self._canonical[field_name] = deepcopy(
                    value
                )

        self._discard_overlay(
            operation,
            affected,
        )

    def _apply_confirmed(
        self,
        values: dict[str, Any],
    ) -> None:
        for field_name, value in values.items():
            self._canonical[field_name] = deepcopy(
                value
            )

            self._refresh_field(
                field_name,
            )

    def _restore_assignment(
        self,
        values: dict[str, Any],
    ) -> None:
        for field_name, value in values.items():
            self._set_visible(
                field_name,
                value,
            )

    def _refresh_field(
        self,
        field_name: str,
    ) -> None:
        overlays = self._overlays.get(
            field_name
        )

        if overlays:
            value = overlays[-1][1]
        else:
            value = self._canonical.get(
                field_name
            )

        self._set_visible(
            field_name,
            value,
        )

    def _set_visible(
        self,
        field_name: str,
        value: Any,
    ) -> None:
        copied = _copy_value(value)

        self._visible[field_name] = copied

        if getattr(
            self,
            field_name,
            None,
        ) == copied:
            return

        self._applying = True

        try:
            setattr(
                self,
                field_name,
                copied,
            )
        except Exception:
            logger.exception(
                (
                    "State property callback failed: "
                    "schema=%s row_id=%s field=%s"
                ),
                self._schema.name,
                self._row_id,
                field_name,
            )
        finally:
            self._applying = False

    def _manager(
        self,
    ) -> BaseStateManager:
        manager = self._manager_ref()

        if manager is None:
            raise StoreStateError(
                "The state manager is no longer available"
            )

        return manager

    @property
    def schema(
        self,
    ) -> SchemaT:
        return self._schema

    @property
    def fields(
        self,
    ) -> dict[str, BaseField[Any]]:
        return self._schema.fields()

    @property
    def row_id(
        self,
    ) -> str | None:
        return self._row_id

    @property
    def address(
        self,
    ) -> Address[Any] | None:
        return self._address

    def __repr__(
        self,
    ) -> str:
        return (
            f"{type(self).__name__}("
            f"schema={self._schema.name!r}, "
            f"row_id={self._row_id!r}"
            ")"
        )


def entity_type(
    schema_type: type[SchemaT],
) -> type[EntityState[SchemaT]]:
    cached = _ENTITY_TYPES.get(
        schema_type
    )

    if cached is not None:
        return cast(
            type[EntityState[SchemaT]],
            cached,
        )

    schema = schema_type()
    namespace: dict[str, Any] = {}
    names = set(
        schema.field_names()
    )

    for field_name in names:
        _validate_name(
            schema,
            field_name,
        )

        namespace[field_name] = ObjectProperty(
            None,
            allownone=True,
            rebind=True,
        )

    for (
        field_name,
        field,
    ) in schema.foreign_key_fields().items():
        relation = field.relation_name(
            field_name
        )

        if relation == field_name:
            continue

        _validate_name(
            schema,
            relation,
        )

        if relation in names:
            raise StoreStateError(
                (
                    f"Relation {relation!r} conflicts "
                    f"with a field in {schema.name}"
                )
            )

        names.add(
            relation
        )

        namespace[relation] = ObjectProperty(
            None,
            allownone=True,
            rebind=True,
        )

    signal_names = schema.signal_names()

    namespace["__events__"] = (
        *StateEvents.__events__,
        *(
            f"on_{signal_name}"
            for signal_name in signal_names
        ),
    )

    for signal_name in signal_names:
        _validate_name(
            schema,
            signal_name,
        )

        if signal_name in names:
            raise StoreStateError(
                (
                    f"Signal {signal_name!r} conflicts "
                    f"with a field or relation in "
                    f"{schema.name}"
                )
            )

        namespace[signal_name] = _signal_method(
            signal_name
        )

        namespace[
            f"on_{signal_name}"
        ] = _signal_event

    state_type = type(
        f"{schema.name}State",
        (EntityState,),
        namespace,
    )

    typed_state_type = cast(
        type[EntityState[SchemaT]],
        state_type,
    )

    _ENTITY_TYPES[schema_type] = cast(
        type[EntityState[Any]],
        typed_state_type,
    )

    return typed_state_type


def _signal_method(
    signal_name: str,
):
    def emit(
        self: EntityState[Any],
        data: Any = None,
    ) -> StateOperation:
        return self._emit_signal(
            signal_name,
            data,
        )

    emit.__name__ = signal_name

    return emit


def _signal_event(
    self: EntityState[Any],
    event: Any,
) -> None:
    pass


def _validate_name(
    schema: Schema,
    name: str,
) -> None:
    if (
        name in _RESERVED_NAMES
        or hasattr(
            EntityState,
            name,
        )
    ):
        raise StoreStateError(
            (
                f"State name {name!r} is reserved "
                f"in {schema.name}"
            )
        )


def _copy_value(
    value: Any,
) -> Any:
    if isinstance(
        value,
        EntityState,
    ):
        return value

    return deepcopy(value)
