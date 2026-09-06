from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from kivyfn.store.schema import SingleSchema
from kivyfn.store.state.base import StateEvents
from kivyfn.store.state.entity import EntityState


if TYPE_CHECKING:
    from kivyfn.store.address import Address
    from kivyfn.store.state.manager import BaseStateManager


SchemaT = TypeVar(
    "SchemaT",
    bound=SingleSchema,
)


class SingleStateView(
    StateEvents,
    Generic[SchemaT],
):
    """Internal watch holder for one single-schema entity."""

    def __init__(
        self,
        manager: BaseStateManager,
        schema: SchemaT,
        entity: EntityState[SchemaT],
        **kwargs: Any,
    ) -> None:
        self._manager = manager
        self._schema = schema
        self._entity = entity
        self._address: Address[Any] | None = None
        self._subscription: Any = None
        self._disposed = False

        super().__init__(**kwargs)

        self._manager._register_entity_view(
            self,
            entity,
        )

    def _replace_snapshot(
        self,
        data: Mapping[str, Any],
    ) -> None:
        self._entity._hydrate(
            dict(data),
        )

    def dispose(self) -> None:
        if self._disposed:
            return

        self._disposed = True
        self.synced = False

        self._manager._unregister_entity_view(
            self,
            self._entity,
        )

        self._manager._dispose_view(
            self,
        )

    @property
    def schema(self) -> SchemaT:
        return self._schema

    @property
    def address(
        self,
    ) -> Address[Any] | None:
        return self._address

    @property
    def entity(
        self,
    ) -> EntityState[SchemaT]:
        return self._entity

    @property
    def disposed(self) -> bool:
        return self._disposed
