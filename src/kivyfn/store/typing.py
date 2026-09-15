# src/kivyfn/store/typing.py
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Generic, TypeVar

from kivyfn.store.schema import Schema
from kivyfn.store.state.reactive.collection import CollectionState
from kivyfn.store.state.reactive.query import QueryCollectionState
from kivyfn.store.state.reactive.state import State

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["CollectionStateType", "QueryStateType", "StateType"]

RowT = TypeVar("RowT", bound=State[Any])


class StateType(State[Schema]):
    """Extend a generated State with typed fields and local behavior.

    Declare schema fields using annotations without assigning values. Pass
    the subclass to state_template(state_type=...) to install its methods,
    event handlers, and local Kivy properties on the managed State.
    """


class CollectionStateType(CollectionState[Any], Generic[RowT]):
    """Editable collection extension with typed rows and membership events."""

    if TYPE_CHECKING:

        @property
        def rows(self) -> Mapping[str, RowT]: ...

        def __getitem__(self, row_id: str) -> RowT: ...

        def get(self, row_id: str) -> RowT | None: ...

        def values(self) -> tuple[RowT, ...]: ...

        def items(self) -> tuple[tuple[str, RowT], ...]: ...

        def canonical_row(self, row_id: str) -> RowT | None: ...

    def on_enter(self, row_id: str, state: RowT) -> None:
        pass

    def on_leave(self, row_id: str, state: RowT) -> None:
        pass

    def on_insert(self, row_id: str, state: RowT) -> None:
        pass

    def on_remove(self, row_id: str, state: RowT) -> None:
        pass


class QueryStateType(QueryCollectionState[Any], Generic[RowT]):
    """Query extension with read-only membership and typed reactive rows.

    Keys are root row IDs. Each row has the declared type, including any
    flattened joined fields. Membership follows the query; writes to row
    fields use the normal State API.
    """

    if TYPE_CHECKING:

        @property
        def rows(self) -> Mapping[str, RowT]: ...

        def __getitem__(self, row_id: str) -> RowT: ...

        def get(self, row_id: str) -> RowT | None: ...

        def values(self) -> tuple[RowT, ...]: ...

        def items(self) -> tuple[tuple[str, RowT], ...]: ...

        def canonical_row(self, row_id: str) -> RowT | None: ...

    def on_enter(self, row_id: str, state: RowT) -> None:
        pass

    def on_leave(self, row_id: str, state: RowT) -> None:
        pass
