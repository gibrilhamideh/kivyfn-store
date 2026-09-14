# src/kivyfn/store/state/reactive/factory.py
from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, TypeVar, cast

from kivyfn.store.error import StoreStateError
from kivyfn.store.query.shape import (
    QueryFieldShape,
    QueryShape,
    query_field_metadata,
)
from kivyfn.store.schema import Schema
from kivyfn.store.schema.fields import BaseField
from kivyfn.store.state.reactive.property import state_property
from kivyfn.store.state.reactive.state import State, StateManagerProtocol

if TYPE_CHECKING:
    from kivyfn.store.state.transaction.transaction import StateTransaction


SchemaT = TypeVar("SchemaT", bound=Schema)

SchemaResolver = Callable[[str], Schema]


class StateFactory:
    """Build and cache reactive State types."""

    _schema_types: dict[type[Schema], type[State[Any]]] = {}

    _query_types: dict[tuple[type[Schema], QueryShape], type[State[Any]]] = {}

    def create(
        self,
        manager: StateManagerProtocol,
        schema: SchemaT,
        row_id: str | None,
    ) -> State[SchemaT]:
        state_type = self.type_for(type(schema))

        return state_type(manager=manager, schema=schema, row_id=row_id)

    def create_query(
        self,
        manager: StateManagerProtocol,
        schema: SchemaT,
        row_id: str,
        shape: QueryShape,
        resolve_schema: SchemaResolver,
    ) -> State[SchemaT]:
        state_type = self.query_type_for(type(schema), shape, resolve_schema)

        return state_type(manager=manager, schema=schema, row_id=row_id)

    def type_for(self, schema_type: type[SchemaT]) -> type[State[SchemaT]]:
        cached = self._schema_types.get(schema_type)

        if cached is not None:
            return cast(type[State[SchemaT]], cached)

        schema = schema_type()
        fields = schema.fields()

        generated = self._build_type(
            name=f"{_schema_stem(schema_type)}State",
            schema=schema,
            fields=fields,
            query_shape=None,
        )

        self._schema_types[schema_type] = generated

        return cast(type[State[SchemaT]], generated)

    def query_type_for(
        self,
        schema_type: type[SchemaT],
        shape: QueryShape,
        resolve_schema: SchemaResolver,
    ) -> type[State[SchemaT]]:
        key = (cast(type[Schema], schema_type), shape)
        cached = self._query_types.get(key)

        if cached is not None:
            return cast(type[State[SchemaT]], cached)

        root = resolve_schema(shape.root_schema)

        if type(root) is not schema_type:
            raise StoreStateError(
                "Query shape root schema does not match State schema"
            )

        fields = query_field_metadata(shape, resolve_schema)

        generated = self._build_type(
            name=f"{_schema_stem(schema_type)}QueryState",
            schema=root,
            fields=fields,
            query_shape=shape,
        )

        self._query_types[key] = generated

        return cast(type[State[SchemaT]], generated)

    def _build_type(
        self,
        *,
        name: str,
        schema: Schema,
        fields: Mapping[str, BaseField[Any]],
        query_shape: QueryShape | None,
    ) -> type[State[Any]]:
        field_metadata = MappingProxyType(dict(fields))

        query_fields: Mapping[str, QueryFieldShape]

        if query_shape is None:
            query_fields = MappingProxyType({})
        else:
            query_fields = MappingProxyType(
                {field.name: field for field in query_shape.fields}
            )

        namespace: dict[str, Any] = {
            "_state_field_names": frozenset(fields),
            "_state_field_metadata": field_metadata,
            "_query_shape": query_shape,
            "_query_field_shapes": query_fields,
        }

        used_names = set(fields)

        for field_name, field in fields.items():
            _validate_name(schema, field_name)

            namespace[field_name] = state_property(field)

        signal_names = schema.signal_names()

        namespace["__events__"] = (
            *State.__events__,
            *(f"on_{signal_name}" for signal_name in signal_names),
        )

        for signal_name in signal_names:
            event_name = f"on_{signal_name}"

            _validate_name(schema, signal_name)
            _validate_name(schema, event_name)

            if signal_name in used_names:
                raise StoreStateError(
                    f"Signal {signal_name!r} conflicts with a State field "
                    f"in {schema.name}"
                )

            used_names.add(signal_name)

            namespace[signal_name] = _signal_method(signal_name)
            namespace[event_name] = _signal_event

        return cast(type[State[Any]], type(name, (State,), namespace))


def query_field_source(state: State[Any], name: str) -> QueryFieldShape | None:
    """Return source metadata for one query-shaped State field."""
    fields = cast(
        Mapping[str, QueryFieldShape],
        getattr(type(state), "_query_field_shapes", {}),
    )

    return fields.get(name)


def _schema_stem(schema_type: type[Schema]) -> str:
    name = schema_type.__name__

    if name.endswith("Schema"):
        name = name[:-6]

    return name


def _validate_name(schema: Schema, name: str) -> None:
    if name.startswith("_") or hasattr(State, name):
        raise StoreStateError(
            f"State name {name!r} is reserved in {schema.name}"
        )


def _signal_method(
    signal_name: str,
) -> Callable[[State[Any], Any], StateTransaction]:
    def emit(state: State[Any], data: Any = None) -> StateTransaction:
        return state._emit_signal(signal_name, data)

    emit.__name__ = signal_name

    return emit


def _signal_event(state: State[Any], data: Any = None) -> None:
    pass
