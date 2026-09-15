# src/kivyfn/store/state/reactive/query.py
from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from types import MappingProxyType
from typing import Generic, TypeVar

from kivy.event import EventDispatcher
from kivy.properties import AliasProperty, BooleanProperty, NumericProperty
from kivyfn.logging import get_logger

from kivyfn.store.error import StoreStateError
from kivyfn.store.query import Query
from kivyfn.store.query.shape import QueryShape
from kivyfn.store.schema import ManySchema
from kivyfn.store.state.reactive.collection import MembershipDelta
from kivyfn.store.state.reactive.dispatch import set_property
from kivyfn.store.state.reactive.state import State

logger = get_logger(__name__)

SchemaT = TypeVar("SchemaT", bound=ManySchema)

QueryRowFactory = Callable[[str], State[SchemaT]]


class QueryCollectionState(EventDispatcher, Generic[SchemaT]):
    """Read-only reactive collection derived from one immutable Query."""

    synced = BooleanProperty(False)
    _rows_version = NumericProperty(0)

    __events__ = ("on_enter", "on_leave")

    def __init__(
        self,
        schema: SchemaT,
        query: Query[SchemaT],
        shape: QueryShape,
        row_factory: QueryRowFactory[SchemaT],
    ) -> None:
        super().__init__()

        if not shape.live_compatible:
            raise ValueError("Query does not preserve live root-row identity")

        if shape.root_schema != schema.name:
            raise ValueError("Query shape root does not match query schema")

        self._schema = schema
        self._query = query
        self._shape = shape
        self._row_factory = row_factory

        self._rows: dict[str, State[SchemaT]] = {}
        self._rows_view: Mapping[str, State[SchemaT]] = MappingProxyType(
            self._rows
        )

    @property
    def schema(self) -> SchemaT:
        return self._schema

    @property
    def query(self) -> Query[SchemaT]:
        return self._query

    @property
    def shape(self) -> QueryShape:
        return self._shape

    def _get_rows(self) -> Mapping[str, State[SchemaT]]:
        return self._rows_view

    rows = AliasProperty(_get_rows, None, bind=("_rows_version",), cache=True)

    def __getitem__(self, row_id: str) -> State[SchemaT]:
        return self._rows[row_id]

    def __iter__(self) -> Iterator[str]:
        return iter(self._rows)

    def __len__(self) -> int:
        return len(self._rows)

    def __contains__(self, row_id: object) -> bool:
        return row_id in self._rows

    def get(self, row_id: str) -> State[SchemaT] | None:
        return self._rows.get(row_id)

    def keys(self) -> tuple[str, ...]:
        return tuple(self._rows)

    def values(self) -> tuple[State[SchemaT], ...]:
        return tuple(self._rows.values())

    def items(self) -> tuple[tuple[str, State[SchemaT]], ...]:
        return tuple(self._rows.items())

    def canonical_row(self, row_id: str) -> State[SchemaT] | None:
        return self._rows.get(row_id)

    def sync_rows(self, synced: bool) -> None:
        for state in self._rows.values():
            state.set_owner_synced(self, synced)
        set_property(self, "synced", synced)

    def enter(self, row_id: str) -> MembershipDelta[SchemaT]:
        self._validate_row_id(row_id)

        existing = self._rows.get(row_id)

        if existing is not None:
            return MembershipDelta()

        state = self._create_row(row_id)
        self._rows[row_id] = state
        state.set_owner_synced(self, bool(self.synced))

        return MembershipDelta(entered=((row_id, state),))

    def leave(self, row_id: str) -> MembershipDelta[SchemaT]:
        state = self._rows.pop(row_id, None)

        if state is None:
            return MembershipDelta()

        state.set_owner_synced(self, False)
        return MembershipDelta(left=((row_id, state),))

    def set_membership(
        self, row_ids: Iterable[str]
    ) -> MembershipDelta[SchemaT]:
        ordered_ids = tuple(row_ids)

        for row_id in ordered_ids:
            self._validate_row_id(row_id)

        if len(ordered_ids) != len(set(ordered_ids)):
            raise ValueError(
                "Query membership cannot contain duplicate row IDs"
            )

        target_ids = set(ordered_ids)

        entered: list[tuple[str, State[SchemaT]]] = []
        left: list[tuple[str, State[SchemaT]]] = []

        for row_id in tuple(self._rows):
            if row_id in target_ids:
                continue

            state = self._rows.pop(row_id)
            state.set_owner_synced(self, False)
            left.append((row_id, state))

        for row_id in ordered_ids:
            if row_id in self._rows:
                continue

            state = self._create_row(row_id)
            self._rows[row_id] = state
            state.set_owner_synced(self, bool(self.synced))

            entered.append((row_id, state))

        return MembershipDelta(entered=tuple(entered), left=tuple(left))

    def publish_membership(self, delta: MembershipDelta[SchemaT]) -> None:
        if not delta.changed:
            return

        set_property(self, "_rows_version", self._rows_version + 1)

        for row_id, state in delta.left:
            self._dispatch_membership("on_leave", row_id, state)

        for row_id, state in delta.entered:
            self._dispatch_membership("on_enter", row_id, state)

    def _create_row(self, row_id: str) -> State[SchemaT]:
        self._validate_row_id(row_id)

        state = self._row_factory(row_id)

        if state.row_id != row_id:
            raise StoreStateError(
                "State row ID does not match query membership"
            )

        return state

    def _validate_row_id(self, row_id: str) -> None:
        if not row_id:
            raise ValueError("Query row ID must be a non-empty string")

    def _dispatch_membership(
        self, event: str, row_id: str, state: State[SchemaT]
    ) -> None:
        try:
            self.dispatch(event, row_id, state)
        except Exception:
            logger.exception(
                "Query membership callback failed: "
                "schema=%s event=%s row_id=%s",
                self._schema.name,
                event,
                row_id,
            )

    def on_enter(self, row_id: str, state: State[SchemaT]) -> None:
        pass

    def on_leave(self, row_id: str, state: State[SchemaT]) -> None:
        pass

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"schema={self._schema.name!r}, "
            f"rows={len(self._rows)}, "
            f"query={self._query!r}"
            ")"
        )
