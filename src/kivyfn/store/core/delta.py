# src/kivyfn/store/core/delta.py
from __future__ import annotations

from collections import defaultdict
from typing import Any

from kivyfn.store.core.operations import (
    InsertOperation,
    Operation,
    RemoveOperation,
    SetRowOperation,
)
from kivyfn.store.error import (
    RowAlreadyExistsError,
    RowErrorDetail,
    RowNotFoundError,
    RowOperationConflictError,
)
from kivyfn.store.node import Node


class Delta:
    """
    Normalized future row state for one transaction.

    Delta validates structural row operations and exposes the logical row state
    that will exist if the transaction commits.

    Relational constraints are validated separately by ConstraintGuard.
    """

    def __init__(
        self,
        operations: list[Operation],
    ) -> None:
        self._inserted: defaultdict[
            Node,
            dict[str, dict[str, Any]],
        ] = defaultdict(dict)

        self._removed: defaultdict[
            Node,
            set[str],
        ] = defaultdict(set)

        self._replaced: defaultdict[
            Node,
            set[str],
        ] = defaultdict(set)

        self._updated: defaultdict[
            Node,
            defaultdict[str, dict[str, Any]],
        ] = defaultdict(lambda: defaultdict(dict))

        self._touched_nodes: set[Node] = set()

        self._touched_fields: defaultdict[
            Node,
            set[str],
        ] = defaultdict(set)

        for operation in operations:
            self._touched_nodes.add(operation.node)

            if isinstance(
                operation,
                InsertOperation,
            ):
                self._insert(
                    operation.node,
                    operation.id,
                    operation.value,
                )
                continue

            if isinstance(
                operation,
                RemoveOperation,
            ):
                self._remove(
                    operation.node,
                    operation.id,
                )
                continue

            if isinstance(
                operation,
                SetRowOperation,
            ):
                self._update(
                    operation.node,
                    operation.id,
                    operation.key,
                    operation.new_value,
                )

    # -------------------------------------------------
    # Structural normalization
    # -------------------------------------------------

    def _insert(
        self,
        node: Node,
        row_id: str,
        row: dict[str, Any],
    ) -> None:
        if row_id in self._inserted[node]:
            raise RowOperationConflictError(
                (
                    f"Row {row_id!r} is inserted more than once "
                    "in the same transaction"
                ),
                detail=RowErrorDetail(
                    schema=node.schema.name,
                    address=node.address.parts,
                    row_id=row_id,
                    operation="insert",
                    conflicting_operation="insert",
                ),
            )

        was_removed = row_id in self._removed[node]

        if row_id in node.value and not was_removed:
            raise RowAlreadyExistsError(
                (
                    f"Row {row_id!r} already exists "
                    f"in schema {node.schema.name}"
                ),
                detail=RowErrorDetail(
                    schema=node.schema.name,
                    address=node.address.parts,
                    row_id=row_id,
                    operation="insert",
                ),
            )

        if was_removed:
            self._removed[node].remove(row_id)
            self._replaced[node].add(row_id)

        self._inserted[node][row_id] = dict(row)

        self._updated[node].pop(
            row_id,
            None,
        )

        self._touched_fields[node].update(row)

    def _remove(
        self,
        node: Node,
        row_id: str,
    ) -> None:
        if row_id in self._inserted[node]:
            del self._inserted[node][row_id]

            if row_id in self._replaced[node]:
                self._replaced[node].remove(row_id)
                self._removed[node].add(row_id)

            self._updated[node].pop(
                row_id,
                None,
            )
            return

        if row_id in self._removed[node]:
            raise RowOperationConflictError(
                (
                    f"Row {row_id!r} is removed more than once "
                    "in the same transaction"
                ),
                detail=RowErrorDetail(
                    schema=node.schema.name,
                    address=node.address.parts,
                    row_id=row_id,
                    operation="remove",
                    conflicting_operation="remove",
                ),
            )

        if row_id not in node.value:
            raise RowNotFoundError(
                (
                    f"Cannot remove nonexistent row {row_id!r} "
                    f"from schema {node.schema.name}"
                ),
                detail=RowErrorDetail(
                    schema=node.schema.name,
                    address=node.address.parts,
                    row_id=row_id,
                    operation="remove",
                ),
            )

        self._removed[node].add(row_id)

        self._updated[node].pop(
            row_id,
            None,
        )

    def _update(
        self,
        node: Node,
        row_id: str,
        field: str,
        value: Any,
    ) -> None:
        self._touched_fields[node].add(field)

        if row_id in self._inserted[node]:
            self._inserted[node][row_id][field] = value
            return

        if row_id in self._removed[node]:
            raise RowOperationConflictError(
                (
                    f"Cannot update removed row {row_id!r} "
                    f"in schema {node.schema.name}"
                ),
                detail=RowErrorDetail(
                    schema=node.schema.name,
                    address=node.address.parts,
                    row_id=row_id,
                    operation="setrow",
                    conflicting_operation="remove",
                ),
            )

        if row_id not in node.value:
            raise RowNotFoundError(
                (
                    f"Cannot update nonexistent row {row_id!r} "
                    f"in schema {node.schema.name}"
                ),
                detail=RowErrorDetail(
                    schema=node.schema.name,
                    address=node.address.parts,
                    row_id=row_id,
                    operation="setrow",
                ),
            )

        self._updated[node][row_id][field] = value

    # -------------------------------------------------
    # Logical future-state API
    # -------------------------------------------------

    def exists(
        self,
        node: Node,
        row_id: str,
    ) -> bool:
        if row_id in self._inserted[node]:
            return True

        if row_id in self._removed[node]:
            return False

        return row_id in node.value

    def value(
        self,
        node: Node,
        row_id: str,
        field: str,
    ) -> Any:
        inserted = self._inserted[node].get(row_id)

        if inserted is not None:
            return inserted.get(field)

        if row_id in self._removed[node]:
            return None

        updated = self._updated[node].get(row_id)

        if updated is not None and field in updated:
            return updated[field]

        row = node.value.get(row_id)

        if not isinstance(
            row,
            dict,
        ):
            return None

        return row.get(field)

    def inserted_ids(
        self,
        node: Node,
    ) -> frozenset[str]:
        return frozenset(self._inserted[node])

    def removed_ids(
        self,
        node: Node,
    ) -> frozenset[str]:
        return frozenset(self._removed[node])

    def updated_ids(
        self,
        node: Node,
    ) -> frozenset[str]:
        return frozenset(self._updated[node])

    @property
    def touched_nodes(self) -> frozenset[Node]:
        return frozenset(self._touched_nodes)

    @property
    def removed_nodes(self) -> frozenset[Node]:
        return frozenset(
            node
            for node, row_ids in self._removed.items()
            if row_ids
        )

    def touched_fields(
        self,
        node: Node,
    ) -> frozenset[str]:
        return frozenset(self._touched_fields[node])
