# src/kivyfn/store/state/reactive/query.py
from __future__ import annotations

from collections.abc import Mapping
from functools import partial
from types import MappingProxyType
from typing import TYPE_CHECKING, TypeVar

from kivyfn.store.query import Query
from kivyfn.store.query.shape import QueryShape
from kivyfn.store.schema import ManySchema
from kivyfn.store.state.reactive.base import (
    BaseCollection,
    MembershipDelta,
    RowFactory,
)
from kivyfn.store.state.reactive.dispatch import notification_batch, notify

if TYPE_CHECKING:
    from kivyfn.store.state.reactive.state import State

SchemaT = TypeVar("SchemaT", bound=ManySchema)


class QueryCollectionState(BaseCollection[SchemaT]):
    """Host-controlled row membership with no collection overlay storage.

    Each projected row is a State with its own field data and overlays.
    Membership itself is just the latest confirmed host result.
    """

    def __init__(
        self,
        schema: SchemaT,
        query: Query[SchemaT],
        shape: QueryShape,
        row_factory: RowFactory[SchemaT],
    ) -> None:
        if not shape.live_compatible:
            raise ValueError("Query does not preserve live root-row identity")

        if shape.root_schema != schema.name:
            raise ValueError("Query shape root does not match query schema")

        rows: dict[str, State[SchemaT]] = {}
        super().__init__(schema, row_factory, MappingProxyType(rows))
        self._rows = rows
        self._query = query
        self._shape = shape

    @property
    def query(self) -> Query[SchemaT]:
        return self._query

    @property
    def shape(self) -> QueryShape:
        return self._shape

    def set_canonical(
        self, values: Mapping[str, State[SchemaT] | None]
    ) -> None:
        """Apply host row entries and notify after the current batch."""
        self._require_rows(values)
        entered: list[tuple[str, State[SchemaT]]] = []
        left: list[tuple[str, State[SchemaT]]] = []
        with notification_batch():
            for row_id, state in values.items():
                previous = self._rows.get(row_id)
                if previous is state:
                    continue
                if previous is not None:
                    del self._rows[row_id]
                    previous.set_owner_synced(self, False)
                    left.append((row_id, previous))
                if state is not None:
                    self._rows[row_id] = state
                    state.set_owner_synced(self, bool(self.synced))
                    entered.append((row_id, state))
            delta = MembershipDelta(entered=tuple(entered), left=tuple(left))
            notify(partial(self.publish, delta))

    def replace_canonical(self, values: Mapping[str, State[SchemaT]]) -> None:
        """Keep surviving rows and remove rows absent from the result."""
        changes: dict[str, State[SchemaT] | None] = {
            row_id: None for row_id in self._rows if row_id not in values
        }
        changes.update(values)
        self.set_canonical(changes)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"schema={self._schema.name!r}, "
            f"rows={len(self)}, "
            f"query={self._query!r}"
            ")"
        )
