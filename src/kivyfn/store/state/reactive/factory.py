# src/kivyfn/store/state/reactive/factory.py
from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, TypeVar, cast
from weakref import WeakValueDictionary

from kivyfn.store.error import StoreStateError
from kivyfn.store.query.shape import (
    QueryFieldShape,
    QueryShape,
    query_field_metadata,
)
from kivyfn.store.schema import ManySchema, Schema
from kivyfn.store.schema.fields import BaseField
from kivyfn.store.state.reactive.collection import CollectionState
from kivyfn.store.state.reactive.property import state_property
from kivyfn.store.state.reactive.query import QueryCollectionState
from kivyfn.store.state.reactive.state import State, StateManagerProtocol

if TYPE_CHECKING:
    from kivyfn.store.state.transaction.transaction import StateTransaction


SchemaT = TypeVar("SchemaT", bound=Schema)
ManySchemaT = TypeVar("ManySchemaT", bound=ManySchema)

SchemaResolver = Callable[[str], Schema]


class StateFactory:
    """Build and cache reactive State and collection types."""

    _schema_types: dict[
        tuple[type[Schema], type[State[Any]]], type[State[Any]]
    ] = {}

    _query_types: WeakValueDictionary[
        tuple[type[Schema], QueryShape, type[State[Any]]], type[State[Any]]
    ] = WeakValueDictionary()

    _collection_types: dict[
        tuple[type[ManySchema], type[CollectionState[Any]]],
        type[CollectionState[Any]],
    ] = {}

    _query_collection_types: dict[
        tuple[type[ManySchema], type[QueryCollectionState[Any]]],
        type[QueryCollectionState[Any]],
    ] = {}

    def create(
        self,
        manager: StateManagerProtocol,
        schema: SchemaT,
        row_id: str | None,
        base_type: type[State[Any]] = State,
    ) -> State[SchemaT]:
        state_type = self.type_for(type(schema), base_type)

        return state_type(manager=manager, schema=schema, row_id=row_id)

    def create_query(
        self,
        manager: StateManagerProtocol,
        schema: SchemaT,
        row_id: str,
        shape: QueryShape,
        resolve_schema: SchemaResolver,
        base_type: type[State[Any]] = State,
    ) -> State[SchemaT]:
        state_type = self.query_type_for(
            type(schema), shape, resolve_schema, base_type
        )

        return state_type(manager=manager, schema=schema, row_id=row_id)

    def type_for(
        self, schema_type: type[SchemaT], base_type: type[State[Any]] = State
    ) -> type[State[SchemaT]]:
        key = (schema_type, base_type)
        cached = self._schema_types.get(key)

        if cached is not None:
            return cast(type[State[SchemaT]], cached)

        schema = schema_type()
        fields = schema.fields()

        generated = self._build_type(
            name=f"{_schema_stem(schema_type)}State",
            schema=schema,
            fields=fields,
            query_shape=None,
            base_type=base_type,
        )

        self._schema_types[key] = generated

        return cast(type[State[SchemaT]], generated)

    def query_type_for(
        self,
        schema_type: type[SchemaT],
        shape: QueryShape,
        resolve_schema: SchemaResolver,
        base_type: type[State[Any]] = State,
    ) -> type[State[SchemaT]]:
        key = (cast(type[Schema], schema_type), shape, base_type)
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
            base_type=base_type,
        )

        self._query_types[key] = generated

        return cast(type[State[SchemaT]], generated)

    def collection_type_for(
        self,
        schema_type: type[ManySchemaT],
        base_type: type[CollectionState[Any]] = CollectionState,
    ) -> type[CollectionState[ManySchemaT]]:
        key = (cast(type[ManySchema], schema_type), base_type)
        cached = self._collection_types.get(key)

        if cached is not None:
            return cast(type[CollectionState[ManySchemaT]], cached)

        schema = schema_type()

        generated = self._build_collection_type(
            name=f"{_schema_stem(schema_type)}CollectionState",
            schema=schema,
            base_type=base_type,
            reserved_type=CollectionState,
        )

        self._collection_types[key] = cast(
            type[CollectionState[Any]], generated
        )

        return cast(type[CollectionState[ManySchemaT]], generated)

    def query_collection_type_for(
        self,
        schema_type: type[ManySchemaT],
        base_type: type[QueryCollectionState[Any]] = QueryCollectionState,
    ) -> type[QueryCollectionState[ManySchemaT]]:
        key = (cast(type[ManySchema], schema_type), base_type)
        cached = self._query_collection_types.get(key)

        if cached is not None:
            return cast(type[QueryCollectionState[ManySchemaT]], cached)

        schema = schema_type()

        generated = self._build_collection_type(
            name=f"{_schema_stem(schema_type)}QueryCollectionState",
            schema=schema,
            base_type=base_type,
            reserved_type=QueryCollectionState,
        )

        self._query_collection_types[key] = generated

        return cast(type[QueryCollectionState[ManySchemaT]], generated)

    def _build_type(
        self,
        *,
        name: str,
        schema: Schema,
        fields: Mapping[str, BaseField[Any]],
        query_shape: QueryShape | None,
        base_type: type[State[Any]],
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
            _validate_state_name(schema, field_name, base_type)

            namespace[field_name] = state_property(field)

        signal_names = _signal_names(schema)

        namespace["__events__"] = tuple(
            dict.fromkeys(
                (*_event_names(base_type), *(f"on_{s}" for s in signal_names))
            )
        )

        for signal_name in signal_names:
            event_name = f"on_{signal_name}"

            _validate_state_name(schema, signal_name, base_type)
            _validate_state_name(schema, event_name)

            if signal_name in used_names:
                raise StoreStateError(
                    f"Signal {signal_name!r} conflicts with a State name "
                    f"in {schema.name}"
                )

            if event_name in used_names:
                raise StoreStateError(
                    f"Signal event {event_name!r} conflicts with a State "
                    f"name in {schema.name}"
                )

            used_names.add(signal_name)
            used_names.add(event_name)

            namespace[signal_name] = _signal_method(signal_name)
            _add_signal_handler(
                namespace, base_type, event_name, _signal_event
            )

        return cast(type[State[Any]], type(name, (base_type,), namespace))

    def _build_collection_type(
        self,
        *,
        name: str,
        schema: ManySchema,
        base_type: type[Any],
        reserved_type: type[Any],
    ) -> type[Any]:
        signal_names = _signal_names(schema)
        base_events = _event_names(base_type)

        namespace: dict[str, Any] = {
            "__events__": tuple(
                dict.fromkeys(
                    (*base_events, *(f"on_{s}" for s in signal_names))
                )
            )
        }

        for signal_name in signal_names:
            event_name = f"on_{signal_name}"

            if hasattr(reserved_type, event_name):
                raise StoreStateError(
                    f"Signal event {event_name!r} conflicts with "
                    f"{base_type.__name__} in {schema.name}"
                )

            _add_signal_handler(
                namespace, base_type, event_name, _collection_signal_event
            )

        return type(name, (base_type,), namespace)


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


def _signal_names(schema: Schema) -> tuple[str, ...]:
    signal_names = tuple(sorted(schema.signal_names()))

    for signal_name in signal_names:
        if signal_name.startswith("on_"):
            raise StoreStateError(
                f"Signal name {signal_name!r} is reserved in {schema.name}"
            )

    return signal_names


def _validate_state_name(
    schema: Schema, name: str, base_type: type[State[Any]] = State
) -> None:
    if name.startswith("_") or hasattr(base_type, name):
        raise StoreStateError(
            f"State name {name!r} is reserved or conflicts with "
            f"{base_type.__name__} in {schema.name}"
        )


def _signal_method(
    signal_name: str,
) -> Callable[[State[Any], Any], StateTransaction]:
    def emit(state: State[Any], data: Any = None) -> StateTransaction:
        return state._emit_signal(signal_name, data)

    emit.__name__ = signal_name

    return emit


def _signal_event(_state: State[Any], data: Any = None) -> None:
    pass


def _collection_signal_event(
    _collection: Any, row_id: str, data: Any = None
) -> None:
    pass


def _event_names(base_type: type[Any]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            name
            for parent in reversed(base_type.__mro__)
            for name in parent.__dict__.get("__events__", ())
        )
    )


def _add_signal_handler(
    namespace: dict[str, Any],
    base_type: type[Any],
    event_name: str,
    default: Callable[..., None],
) -> None:
    if not hasattr(base_type, event_name):
        namespace[event_name] = default
    elif not callable(getattr(base_type, event_name)):
        raise StoreStateError(
            f"Signal event {event_name!r} must be a method on "
            f"{base_type.__name__}"
        )
