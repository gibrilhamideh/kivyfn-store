# src/kivyfn/store/state/registry.py
from __future__ import annotations

from typing import Any, TypeVar, cast, overload
from weakref import WeakValueDictionary

from kivyfn.store.error import StoreStateError
from kivyfn.store.query import Query
from kivyfn.store.query.shape import QueryShape
from kivyfn.store.schema import ManySchema, Schema, SingleSchema
from kivyfn.store.state.reactive.collection import CollectionState
from kivyfn.store.state.reactive.factory import StateFactory
from kivyfn.store.state.reactive.query import QueryCollectionState
from kivyfn.store.state.reactive.state import State, StateManagerProtocol


SchemaT = TypeVar("SchemaT", bound=Schema)

SingleSchemaT = TypeVar("SingleSchemaT", bound=SingleSchema)

ManySchemaT = TypeVar("ManySchemaT", bound=ManySchema)


class StateRegistry:
    """Own reactive State identity within one StateManager."""

    def __init__(self, manager: Any) -> None:
        self._manager = manager
        self._factory = StateFactory()

        self._states: WeakValueDictionary[object, object] = (
            WeakValueDictionary()
        )

        self._rows: WeakValueDictionary[
            tuple[type[Schema], str], State[Any]
        ] = WeakValueDictionary()

        self._query_rows: WeakValueDictionary[
            tuple[QueryShape, str], State[Any]
        ] = WeakValueDictionary()

        self._schemas: dict[type[Schema], Schema] = {}

        self._schema_types: dict[str, type[Schema]] = {}

    @overload
    def create(self, source: type[SingleSchemaT]) -> State[SingleSchemaT]: ...

    @overload
    def create(
        self, source: type[ManySchemaT]
    ) -> CollectionState[ManySchemaT]: ...

    @overload
    def create(
        self, source: Query[ManySchemaT]
    ) -> QueryCollectionState[ManySchemaT]: ...

    def create(
        self, source: (type[SingleSchema] | type[ManySchema] | Query[Any])
    ) -> Any:
        existing = self._states.get(source)

        if existing is not None:
            return existing

        if isinstance(source, Query):
            state = self._create_query(source)
        elif isinstance(source, type) and issubclass(source, SingleSchema):
            state = self._create_state(source)
        elif isinstance(source, type) and issubclass(source, ManySchema):
            state = self._create_collection(source)
        else:
            raise TypeError(
                "State source must be a SingleSchema, ManySchema, or Query"
            )

        self._states[source] = state

        return state

    def schema(self, schema_type: type[SchemaT]) -> SchemaT:
        existing = self._schemas.get(schema_type)

        if existing is not None:
            return cast(SchemaT, existing)

        schema = schema_type()

        self._schemas[schema_type] = schema
        self._schema_types[schema.name] = schema_type

        return schema

    def resolve_schema(self, name: str) -> Schema:
        schema_type = self._schema_types.get(name)

        if schema_type is None:
            schema_type = _find_schema_type(name)
            self._schema_types[name] = schema_type

        return self.schema(schema_type)

    def create_row(
        self, schema_type: type[SchemaT], row_id: str
    ) -> State[SchemaT]:
        _validate_row_id(row_id)

        key = (cast(type[Schema], schema_type), row_id)
        existing = self._rows.get(key)

        if existing is not None:
            return cast(State[SchemaT], existing)

        schema = self.schema(schema_type)
        state = self._factory.create(
            manager=cast(StateManagerProtocol, self._manager),
            schema=schema,
            row_id=row_id,
        )

        self._rows[key] = cast(State[Any], state)

        return state

    def create_query_row(
        self, schema_type: type[SchemaT], row_id: str, shape: QueryShape
    ) -> State[SchemaT]:
        _validate_row_id(row_id)

        key = (shape, row_id)
        existing = self._query_rows.get(key)

        if existing is not None:
            return cast(State[SchemaT], existing)

        schema = self.schema(schema_type)
        state = self._factory.create_query(
            manager=cast(StateManagerProtocol, self._manager),
            schema=schema,
            row_id=row_id,
            shape=shape,
            resolve_schema=self.resolve_schema,
        )

        self._query_rows[key] = cast(State[Any], state)

        return state

    def _create_state(
        self, schema_type: type[SingleSchemaT]
    ) -> State[SingleSchemaT]:
        schema = self.schema(schema_type)

        return self._factory.create(
            manager=cast(StateManagerProtocol, self._manager),
            schema=schema,
            row_id=None,
        )

    def _create_collection(
        self, schema_type: type[ManySchemaT]
    ) -> CollectionState[ManySchemaT]:
        schema = self.schema(schema_type)

        return CollectionState(
            manager=self._manager,
            schema=schema,
            row_factory=lambda row_id: self.create_row(schema_type, row_id),
        )

    def _create_query(
        self, query: Query[ManySchemaT]
    ) -> QueryCollectionState[ManySchemaT]:
        schema_type = self._query_schema_type(query)
        schema = self.schema(schema_type)
        shape = QueryShape.build(query, schema, self.resolve_schema)

        if not shape.live_compatible:
            raise StoreStateError(
                "Query does not preserve live root-row identity"
            )

        return QueryCollectionState(
            schema=schema,
            query=query,
            shape=shape,
            row_factory=lambda row_id: self.create_query_row(
                schema_type, row_id, shape
            ),
        )

    def _query_schema_type(
        self, query: Query[ManySchemaT]
    ) -> type[ManySchemaT]:
        source = query.source

        if isinstance(source, type) and issubclass(source, ManySchema):
            return cast(type[ManySchemaT], source)

        if isinstance(source, str):
            schema = self.resolve_schema(source)

            if isinstance(schema, ManySchema):
                return cast(type[ManySchemaT], type(schema))

        raise StoreStateError(
            "Live Query State requires a ManySchema class "
            "or schema name as its root"
        )


def _validate_row_id(row_id: str) -> None:
    if not isinstance(row_id, str) or not row_id:
        raise ValueError("State row ID must be a non-empty string")


def _find_schema_type(name: str) -> type[Schema]:
    for schema_type in _schema_subclasses(Schema):
        schema = schema_type()

        if schema.name == name:
            return schema_type

    raise StoreStateError(f"Schema {name!r} is not loaded in this process")


def _schema_subclasses(schema_type: type[Schema]) -> tuple[type[Schema], ...]:
    result: list[type[Schema]] = []

    for child in schema_type.__subclasses__():
        result.append(child)
        result.extend(_schema_subclasses(child))

    return tuple(result)
