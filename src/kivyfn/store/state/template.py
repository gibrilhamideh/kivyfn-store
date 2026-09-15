# src/kivyfn/store/state/template.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, TypeVar, get_args, get_origin, overload

from kivyfn.store.client.subscription import SubscriptionSource
from kivyfn.store.query import Query
from kivyfn.store.schema import ManySchema, SingleSchema
from kivyfn.store.state.reactive.collection import CollectionState
from kivyfn.store.state.reactive.query import QueryCollectionState
from kivyfn.store.state.reactive.state import State
from kivyfn.store.typing import CollectionStateType, QueryStateType

ReactiveTarget = State[Any] | CollectionState[Any] | QueryCollectionState[Any]
TargetT = TypeVar("TargetT", bound=ReactiveTarget, covariant=True)
StateT = TypeVar("StateT", bound=State[Any])
CollectionT = TypeVar("CollectionT", bound=CollectionStateType[Any])
QueryT = TypeVar("QueryT", bound=QueryStateType[Any])
SingleSchemaT = TypeVar("SingleSchemaT", bound=SingleSchema)
ManySchemaT = TypeVar("ManySchemaT", bound=ManySchema)
CollectionType = type[CollectionStateType[Any]] | type[QueryStateType[Any]]


@dataclass(frozen=True, slots=True)
class StateTemplate(Generic[TargetT]):
    """Immutable client-side construction recipe; owns no managed objects.

    Prefer state_template() to infer the complete result type. Configuration
    is applied by each StateManager independently, before it creates objects
    for the source. Templates never become subscription or row cache keys.
    """

    source: SubscriptionSource
    state_type: type[State[Any]] | None = None
    collection_type: CollectionType | None = None

    def __post_init__(self) -> None:
        source = self.source
        if isinstance(source, Query):
            collection_base = QueryStateType
        elif isinstance(source, type) and issubclass(source, ManySchema):
            collection_base = CollectionStateType
        elif isinstance(source, type) and issubclass(source, SingleSchema):
            collection_base = None
        else:
            raise TypeError(
                "State source must be a SingleSchema, ManySchema, or Query"
            )

        if self.state_type is not None:
            _require_subclass(self.state_type, State, "state_type")
        if self.collection_type is None:
            return
        if collection_base is None:
            raise TypeError(
                "SingleSchema templates cannot set collection_type"
            )
        _require_subclass(
            self.collection_type, collection_base, "collection_type"
        )
        declared = _declared_row_type(self.collection_type)
        if declared is not None and not issubclass(
            self.state_type or State, declared
        ):
            raise TypeError(
                f"collection_type requires state_type to extend "
                f"{declared.__name__}"
            )


@overload
def state_template(
    source: type[SingleSchemaT], *, state_type: type[StateT]
) -> StateTemplate[StateT]: ...


@overload
def state_template(
    source: type[SingleSchemaT],
) -> StateTemplate[State[SingleSchemaT]]: ...


@overload
def state_template(
    source: type[ManySchemaT],
    *,
    state_type: type[State[Any]] | None = None,
    collection_type: type[CollectionT],
) -> StateTemplate[CollectionT]: ...


@overload
def state_template(
    source: type[ManySchemaT], *, state_type: type[StateT]
) -> StateTemplate[CollectionStateType[StateT]]: ...


@overload
def state_template(
    source: type[ManySchemaT],
) -> StateTemplate[CollectionState[ManySchemaT]]: ...


@overload
def state_template(
    source: Query[Any],
    *,
    state_type: type[State[Any]] | None = None,
    collection_type: type[QueryT],
) -> StateTemplate[QueryT]: ...


@overload
def state_template(
    source: Query[Any], *, state_type: type[StateT]
) -> StateTemplate[QueryStateType[StateT]]: ...


@overload
def state_template(
    source: Query[ManySchemaT],
) -> StateTemplate[QueryCollectionState[ManySchemaT]]: ...


def state_template(
    source: SubscriptionSource,
    *,
    state_type: type[State[Any]] | None = None,
    collection_type: CollectionType | None = None,
) -> StateTemplate[Any]:
    """Describe the State returned by manager.create(template).

    state_type customizes the singleton or each collection/query row.
    collection_type optionally customizes the collection itself. Declare
    typed collections as CollectionStateType[Row] or QueryStateType[Row],
    and supply a matching state_type. Custom constructors must accept the
    inherited constructor arguments and call super().__init__().
    """
    return StateTemplate(source, state_type, collection_type)


def _require_subclass(value: type[Any], base: type[Any], name: str) -> None:
    if not isinstance(value, type) or not issubclass(value, base):
        raise TypeError(f"{name} must be a {base.__name__} subclass")


def _declared_row_type(
    cls: type[Any], substitutions: dict[Any, Any] | None = None
) -> type[State[Any]] | None:
    """Resolve the row contract, including inherited generic containers."""
    substitutions = substitutions or {}
    bases = cls.__dict__.get("__orig_bases__", cls.__bases__)
    for base in bases:
        origin = get_origin(base) or base
        args = tuple(substitutions.get(arg, arg) for arg in get_args(base))
        if origin in (CollectionStateType, QueryStateType):
            row = args[0] if args else None
            if row is not Any and isinstance(row, type):
                return row
        elif isinstance(origin, type):
            parameters = getattr(origin, "__parameters__", ())
            declared = _declared_row_type(origin, dict(zip(parameters, args)))
            if declared is not None:
                return declared
    return None
