from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast

from kivy.properties import DictProperty

from kivyfn.logging import get_logger
from kivyfn.store.query import Query
from kivyfn.store.schema import ManySchema
from kivyfn.store.state.base import StateEvents
from kivyfn.store.state.entity import EntityState
from kivyfn.store.state.operation import StateOperation


if TYPE_CHECKING:
    from kivyfn.store.address import Address
    from kivyfn.store.state.manager import BaseStateManager


logger = get_logger(__name__)


SchemaT = TypeVar(
    "SchemaT",
    bound=ManySchema,
)


class CollectionState(
    StateEvents,
    Generic[SchemaT],
):
    """Live Kivy collection for a many schema or query result."""

    rows = DictProperty(
        {},
        rebind=True,
    )

    __events__ = (
        *StateEvents.__events__,
        "on_insert",
        "on_remove",
    )

    def __init__(
        self,
        manager: BaseStateManager,
        schema: SchemaT,
        query: Query[SchemaT],
        *,
        mutable: bool,
        **kwargs: Any,
    ) -> None:
        self._manager = manager
        self._schema = schema
        self._query = query
        self._address: Address[Any] | None = None
        self._mutable = mutable
        self._subscription: Any = None
        self._disposed = False
        self._optimistic_insertions: set[str] = set()
        self._optimistic_removals: dict[str, EntityState[Any]] = {}

        super().__init__(**kwargs)

        self.rows = {}

    def __eq__(self, other: object) -> bool:
        return self is other

    def __hash__(self) -> int:
        return object.__hash__(self)

    # -------------------------------------------------
    # Mapping API
    # -------------------------------------------------

    def __getitem__(
        self,
        row_id: str,
    ) -> SchemaT:
        return cast(
            SchemaT,
            self.rows[row_id],
        )

    def __setitem__(
        self,
        row_id: str,
        values: Mapping[str, Any],
    ) -> None:
        self._require_mutable()
        self._require_available_id(row_id)

        self._manager._request_insert(
            self,
            row_id,
            dict(values),
            optimistic=True,
        )

    def __delitem__(
        self,
        row_id: str,
    ) -> None:
        self._require_mutable()

        if row_id not in self.rows:
            raise KeyError(row_id)

        self._manager._request_remove(
            self,
            row_id,
            optimistic=True,
        )

    def __iter__(self) -> Iterator[str]:
        return iter(self.rows)

    def __len__(self) -> int:
        return len(self.rows)

    def __contains__(
        self,
        row_id: object,
    ) -> bool:
        return row_id in self.rows

    def get(
        self,
        row_id: str,
    ) -> SchemaT | None:
        entity = self.rows.get(row_id)

        if entity is None:
            return None

        return cast(
            SchemaT,
            entity,
        )

    def keys(self) -> tuple[str, ...]:
        return tuple(self.rows)

    def values(self) -> tuple[SchemaT, ...]:
        return tuple(
            cast(
                SchemaT,
                entity,
            )
            for entity in self.rows.values()
        )

    def items(
        self,
    ) -> tuple[tuple[str, SchemaT], ...]:
        return tuple(
            (
                row_id,
                cast(
                    SchemaT,
                    entity,
                ),
            )
            for row_id, entity in self.rows.items()
        )

    # -------------------------------------------------
    # Confirmed operations
    # -------------------------------------------------

    def insert(
        self,
        row_id: str,
        **values: Any,
    ) -> StateOperation:
        self._require_mutable()
        self._require_available_id(row_id)

        return self._manager._request_insert(
            self,
            row_id,
            values,
            optimistic=False,
        )

    def remove(
        self,
        row_id: str,
    ) -> StateOperation:
        self._require_mutable()

        if row_id not in self.rows:
            raise KeyError(row_id)

        return self._manager._request_remove(
            self,
            row_id,
            optimistic=False,
        )

    # -------------------------------------------------
    # Lifecycle
    # -------------------------------------------------

    def dispose(self) -> None:
        if self._disposed:
            return

        self._disposed = True
        self.synced = False

        self._manager._dispose_view(self)

    # -------------------------------------------------
    # Synchronization
    # -------------------------------------------------

    def _replace_snapshot(
        self,
        data: Mapping[str, Mapping[str, Any]],
        notify: bool,
    ) -> None:
        previous = dict(self.rows)
        current: dict[str, EntityState[Any]] = {}

        for row_id, values in data.items():
            entity = self._manager._entity(
                self._schema,
                row_id,
            )

            self._manager._hydrate_entity(
                entity,
                dict(values),
                self._query,
            )

            if row_id not in self._optimistic_removals:
                current[row_id] = entity

        for row_id in self._optimistic_insertions:
            entity = previous.get(row_id)

            if entity is None:
                entity = self._manager._entity(
                    self._schema,
                    row_id,
                )

            current[row_id] = entity

        self._replace_rows(
            current,
            notify=notify,
        )

    def _insert_entity(
        self,
        entity: EntityState[Any],
        *,
        notify: bool,
        force: bool = False,
    ) -> None:
        row_id = entity.row_id

        if row_id is None:
            raise ValueError(
                "Collection rows require an ID"
            )

        if (
            not force
            and row_id in self._optimistic_removals
        ):
            return

        existing = self.rows.get(row_id)

        if existing is entity:
            return

        updated = dict(self.rows)
        updated[row_id] = entity
        self.rows = updated

        self._manager._register_entity_view(
            self,
            entity,
        )

        if notify:
            self._dispatch_membership(
                "on_insert",
                entity,
            )

    def _remove_entity(
        self,
        row_id: str,
        *,
        notify: bool,
        force: bool = False,
    ) -> EntityState[Any] | None:
        if (
            not force
            and row_id in self._optimistic_insertions
        ):
            entity = self.rows.get(row_id)

            if entity is None:
                return None

            return cast(
                EntityState[Any],
                entity,
            )

        entity = self.rows.get(row_id)

        if entity is None:
            return None

        updated = dict(self.rows)

        updated.pop(
            row_id,
            None,
        )

        self.rows = updated

        typed_entity = cast(
            EntityState[Any],
            entity,
        )

        self._manager._unregister_entity_view(
            self,
            typed_entity,
        )

        if notify:
            self._dispatch_membership(
                "on_remove",
                typed_entity,
            )

        return typed_entity

    def _replace_rows(
        self,
        current: dict[str, EntityState[Any]],
        *,
        notify: bool,
    ) -> None:
        previous = dict(self.rows)

        for row_id, entity in previous.items():
            if row_id not in current:
                self._manager._unregister_entity_view(
                    self,
                    cast(
                        EntityState[Any],
                        entity,
                    ),
                )

        for row_id, entity in current.items():
            if previous.get(row_id) is not entity:
                self._manager._register_entity_view(
                    self,
                    entity,
                )

        self.rows = current

        if not notify:
            return

        for row_id in previous.keys() - current.keys():
            self._dispatch_membership(
                "on_remove",
                cast(
                    EntityState[Any],
                    previous[row_id],
                ),
            )

        for row_id in current.keys() - previous.keys():
            self._dispatch_membership(
                "on_insert",
                current[row_id],
            )

    def _dispatch_membership(
        self,
        event_name: str,
        entity: EntityState[Any],
    ) -> None:
        try:
            self.dispatch(
                event_name,
                entity,
            )
        except Exception:
            logger.exception(
                (
                    "State collection callback failed: "
                    "schema=%s event=%s"
                ),
                self._schema.name,
                event_name,
            )

    # -------------------------------------------------
    # Validation
    # -------------------------------------------------

    def _require_available_id(
        self,
        row_id: str,
    ) -> None:
        if not isinstance(row_id, str) or not row_id:
            raise ValueError(
                "State row ID must be a non-empty string"
            )

        if (
            row_id in self.rows
            or row_id in self._optimistic_insertions
            or row_id in self._optimistic_removals
        ):
            raise KeyError(
                f"State row {row_id!r} already exists"
            )

    def _require_mutable(self) -> None:
        if self._disposed:
            raise RuntimeError(
                "The state collection is disposed"
            )

        if not self._mutable:
            raise TypeError(
                (
                    "Query membership is read-only; "
                    "mutate its source state"
                )
            )

    # -------------------------------------------------
    # Properties
    # -------------------------------------------------

    @property
    def schema(self) -> SchemaT:
        return self._schema

    @property
    def query(self) -> Query[SchemaT]:
        return self._query

    @property
    def address(
        self,
    ) -> Address[Any] | None:
        return self._address

    @property
    def mutable(self) -> bool:
        return self._mutable

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(self.rows)

    @property
    def disposed(self) -> bool:
        return self._disposed

    # -------------------------------------------------
    # Events
    # -------------------------------------------------

    def on_insert(
        self,
        entity: SchemaT,
    ) -> None:
        pass

    def on_remove(
        self,
        entity: SchemaT,
    ) -> None:
        pass
