# src/kivyfn/store/core/delta.py
from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from typing import Any

from kivyfn.store.core.operations import (
    InsertOperation,
    Operation,
    RemoveOperation,
    SetOperation,
    SetRowOperation,
)
from kivyfn.store.error import (
    RowAlreadyExistsError,
    RowErrorDetail,
    RowNotFoundError,
    RowOperationConflictError,
)
from kivyfn.store.events.event import StoreEvent
from kivyfn.store.node import Node


class Delta:
    """The private logical transaction view, updated before running triggers.

    Canonical nodes remain untouched until every operation, relationship,
    final index and durable write has succeeded. Values and event payloads
    have separate ownership, including nested mutable field values.
    """

    def __init__(self, operations: Iterable[Operation] = ()) -> None:
        self.values: dict[Node, dict[str, Any]] = {}
        self.events: list[StoreEvent] = []
        self._removed: dict[Node, set[str]] = {}
        self._inserted: dict[Node, set[str]] = {}
        self._updated: dict[Node, set[str]] = {}
        self._fields: dict[Node, set[str]] = {}

        for operation in operations:
            self.add(operation)

    def add(self, operation: Operation) -> None:
        node = operation.node
        data = self.values.get(node)
        if data is None:
            data = deepcopy(node.value)
            self.values[node] = data

        if isinstance(operation, InsertOperation):
            if operation.id in data:
                if operation.id in self._inserted.get(node, ()):
                    raise RowOperationConflictError(
                        f"Row {operation.id!r} is inserted more than once",
                        detail=self._detail(operation, "insert", "insert"),
                    )
                raise RowAlreadyExistsError(
                    f"Row {operation.id!r} already exists in "
                    f"{node.schema.name}",
                    detail=self._detail(operation, "insert"),
                )
            self._inserted.setdefault(node, set()).add(operation.id)
            self._removed.setdefault(node, set()).discard(operation.id)
            self._fields.setdefault(node, set()).update(operation.value)

        elif isinstance(operation, (RemoveOperation, SetRowOperation)):
            if operation.id not in data:
                name = (
                    "remove"
                    if isinstance(operation, RemoveOperation)
                    else "setrow"
                )
                if operation.id in self._removed.get(node, ()):
                    raise RowOperationConflictError(
                        f"Cannot {name} removed row {operation.id!r}",
                        detail=self._detail(operation, name, "remove"),
                    )
                raise RowNotFoundError(
                    f"Row {operation.id!r} does not exist in "
                    f"{node.schema.name}",
                    detail=self._detail(operation, name),
                )

            if isinstance(operation, RemoveOperation):
                self._removed.setdefault(node, set()).add(operation.id)
                self._inserted.setdefault(node, set()).discard(operation.id)
                self._updated.setdefault(node, set()).discard(operation.id)
            else:
                self._updated.setdefault(node, set()).add(operation.id)
                self._fields.setdefault(node, set()).add(operation.key)

        elif isinstance(operation, SetOperation):
            self._fields.setdefault(node, set()).add(operation.key)

        self.events.append(operation.stage(data))

    def data(self, node: Node) -> dict[str, Any]:
        """Borrow the final logical value; callers must not mutate it."""
        return self.values.get(node, node.value)

    def exists(self, node: Node, row_id: str) -> bool:
        return row_id in self.data(node)

    def value(self, node: Node, row_id: str, field: str) -> Any:
        row = self.data(node).get(row_id)
        return row.get(field) if isinstance(row, dict) else None

    def inserted_ids(self, node: Node) -> frozenset[str]:
        return frozenset(self._inserted.get(node, ()))

    def removed_ids(self, node: Node) -> frozenset[str]:
        return frozenset(self._removed.get(node, ()))

    def updated_ids(self, node: Node) -> frozenset[str]:
        return frozenset(self._updated.get(node, ()))

    @property
    def touched_nodes(self) -> frozenset[Node]:
        return frozenset(self.values)

    @property
    def removed_nodes(self) -> frozenset[Node]:
        return frozenset(node for node in self._removed if self._removed[node])

    def touched_fields(self, node: Node) -> frozenset[str]:
        return frozenset(self._fields.get(node, ()))

    @staticmethod
    def _detail(
        operation: InsertOperation | SetRowOperation | RemoveOperation,
        name: str,
        conflict: str | None = None,
    ) -> RowErrorDetail:
        return RowErrorDetail(
            schema=operation.node.schema.name,
            address=operation.address.parts,
            row_id=operation.id,
            operation=name,
            conflicting_operation=conflict,
        )
