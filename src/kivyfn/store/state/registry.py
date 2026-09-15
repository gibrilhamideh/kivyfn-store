# src/kivyfn/store/state/registry.py
from __future__ import annotations

from typing import Any, Protocol, TypeVar, cast, overload
from weakref import WeakValueDictionary

from kivyfn.store.client.subscription import SubscriptionSource
from kivyfn.store.error import StoreStateError
from kivyfn.store.query import Query
from kivyfn.store.query.shape import QueryShape
from kivyfn.store.schema import ManySchema, Schema, SingleSchema
from kivyfn.store.state.reactive.collection import (
    CollectionManagerProtocol,
    CollectionState,
)
from kivyfn.store.state.reactive.factory import StateFactory
from kivyfn.store.state.reactive.query import QueryCollectionState
from kivyfn.store.state.reactive.state import State, StateManagerProtocol
from kivyfn.store.state.template import CollectionType, StateTemplate, TargetT
from kivyfn.store.typing import CollectionStateType, QueryStateType

SchemaT = TypeVar("SchemaT", bound=Schema)
SingleSchemaT = TypeVar("SingleSchemaT", bound=SingleSchema)
ManySchemaT = TypeVar("ManySchemaT", bound=ManySchema)


class RegistryManagerProtocol(
    StateManagerProtocol, CollectionManagerProtocol, Protocol
):
    pass


class StateRegistry:
    """Own managed identities and their construction types for one manager."""

    def __init__(self, manager: RegistryManagerProtocol) -> None:
        self._manager = manager
        self._factory = StateFactory()
        self._states: WeakValueDictionary[object, Any] = WeakValueDictionary()
        self._rows: WeakValueDictionary[
            tuple[type[Schema], str], State[Any]
        ] = WeakValueDictionary()
        self._query_rows: WeakValueDictionary[
            tuple[QueryShape, str], State[Any]
        ] = WeakValueDictionary()
        self._schemas: dict[type[Schema], Schema] = {}
        self._schema_types: dict[str, type[Schema]] = {}
        self._state_types: dict[
            type[Schema] | QueryShape, type[State[Any]]
        ] = {}
        self._collection_types: dict[SubscriptionSource, CollectionType] = {}
        self._creating: dict[
            SubscriptionSource, type[Schema] | QueryShape | None
        ] = {}

    @overload
    def create(self, source: StateTemplate[TargetT]) -> TargetT: ...

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

    def create(self, source: SubscriptionSource | StateTemplate[Any]) -> Any:
        template = source if isinstance(source, StateTemplate) else None
        if template is not None:
            source = template.source
        source = cast(SubscriptionSource, source)
        if source in self._creating:
            raise StoreStateError(
                "Recursive State creation for the same source"
            )
        self._creating[source] = None
        try:
            return self._create(source, template)
        finally:
            del self._creating[source]

    def _create(
        self, source: SubscriptionSource, template: StateTemplate[Any] | None
    ) -> Any:
        requested_row = template.state_type if template is not None else None
        requested_collection = (
            template.collection_type if template is not None else None
        )
        existing = self._states.get(source)
        shape = None
        if isinstance(source, Query):
            schema_type = self._query_schema_type(source)
            schema = self.schema(schema_type)
            shape = (
                existing.shape
                if existing is not None
                else QueryShape.build(source, schema, self.resolve_schema)
            )
            key = shape
        elif isinstance(source, type) and issubclass(
            source, (SingleSchema, ManySchema)
        ):
            schema_type = source
            schema = self.schema(schema_type)
            key = schema_type
        else:
            raise TypeError(
                "State source must be a SingleSchema, ManySchema, or Query"
            )

        if existing is None and key in self._creating.values():
            raise StoreStateError(
                "Recursive State creation for a shared row identity"
            )
        self._creating[source] = key
        row_base = _select_type(
            self._state_types.get(key), requested_row, State, "state_type"
        )
        if shape is None:
            row_type = self._factory.type_for(schema_type, row_base)
        else:
            row_type = self._factory.query_type_for(
                schema_type, shape, self.resolve_schema, row_base
            )

        if isinstance(schema, SingleSchema):
            target = existing
            if target is None:
                target = row_type(self._manager, schema, None)
        else:
            if isinstance(source, Query):
                default = QueryStateType
            else:
                default = CollectionStateType
            collection_base = _select_type(
                self._collection_types.get(source),
                requested_collection,
                default,
                "collection_type",
            )
            target = existing
            if isinstance(source, Query):
                assert shape is not None
                query_type = self._factory.query_collection_type_for(
                    type(schema), collection_base
                )
                if target is None:
                    target = query_type(
                        schema=schema,
                        query=source,
                        shape=shape,
                        row_factory=lambda row_id: self.create_query_row(
                            type(schema), row_id, shape, base_type=row_base
                        ),
                    )
            else:
                collection_type = self._factory.collection_type_for(
                    type(schema), collection_base
                )
                if target is None:
                    target = collection_type(
                        manager=self._manager,
                        schema=schema,
                        row_factory=lambda row_id: self.create_row(
                            type(schema), row_id, base_type=row_base
                        ),
                    )
            self._collection_types[source] = collection_base
        self._state_types[key] = row_base
        self._states[source] = target
        return target

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
        self,
        schema_type: type[SchemaT],
        row_id: str,
        *,
        base_type: type[State[Any]] | None = None,
    ) -> State[SchemaT]:
        _validate_row_id(row_id)
        row_base = _select_type(
            self._state_types.get(schema_type), base_type, State, "state_type"
        )
        key = (cast(type[Schema], schema_type), row_id)
        existing = self._rows.get(key)
        if existing is not None:
            return cast(State[SchemaT], existing)
        state = self._factory.create(
            self._manager, self.schema(schema_type), row_id, row_base
        )
        self._state_types[schema_type] = row_base
        self._rows[key] = state
        return state

    def find_row(self, schema_name: str, row_id: str) -> State[Any] | None:
        schema_type = self._schema_types.get(schema_name)
        if schema_type is None:
            return None
        return self._rows.get((schema_type, row_id))

    def create_query_row(
        self,
        schema_type: type[SchemaT],
        row_id: str,
        shape: QueryShape,
        *,
        base_type: type[State[Any]] | None = None,
    ) -> State[SchemaT]:
        _validate_row_id(row_id)
        row_base = _select_type(
            self._state_types.get(shape), base_type, State, "state_type"
        )
        key = (shape, row_id)
        existing = self._query_rows.get(key)
        if existing is not None:
            return cast(State[SchemaT], existing)
        state = self._factory.create_query(
            self._manager,
            self.schema(schema_type),
            row_id,
            shape,
            self.resolve_schema,
            row_base,
        )
        self._state_types[shape] = row_base
        self._query_rows[key] = state
        return state

    def _query_schema_type(self, query: Query[Any]) -> type[ManySchema]:
        source = query.source
        if isinstance(source, type) and issubclass(source, ManySchema):
            return source
        if isinstance(source, str):
            schema = self.resolve_schema(source)
            if isinstance(schema, ManySchema):
                return type(schema)
        raise StoreStateError(
            "Live Query State requires a ManySchema class "
            "or schema name as its root"
        )


def _select_type(
    existing: type[Any] | None,
    requested: type[Any] | None,
    default: type[Any],
    name: str,
) -> type[Any]:
    if existing is not None:
        if requested is not None and requested is not existing:
            raise StoreStateError(
                f"Conflicting {name}: already configured as "
                f"{existing.__name__}, requested {requested.__name__}. "
                "Choose custom types before creating this source or its rows."
            )
        return existing
    return requested or default


def _validate_row_id(row_id: str) -> None:
    if not isinstance(row_id, str) or not row_id:
        raise ValueError("State row ID must be a non-empty string")


def _find_schema_type(name: str) -> type[Schema]:
    for schema_type in _schema_subclasses(Schema):
        if schema_type.__name__ == name:
            return schema_type
    raise StoreStateError(f"Schema {name!r} is not loaded in this process")


def _schema_subclasses(schema_type: type[Schema]) -> tuple[type[Schema], ...]:
    result: list[type[Schema]] = []
    for child in schema_type.__subclasses__():
        result.append(child)
        result.extend(_schema_subclasses(child))
    return tuple(result)
