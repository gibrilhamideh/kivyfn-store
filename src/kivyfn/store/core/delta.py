# kivyfn/store/core/delta.py
from __future__ import annotations

from collections import defaultdict
from typing import Any

from kivyfn.store.error import (
    RowAlreadyExistsError,
    RowErrorDetail,
    RowNotFoundError,
    RowOperationConflictError,
)
from kivyfn.store.node import Node

from .operations import (
    InsertOperation,
    RemoveOperation,
    SetRowOperation,
)


class Delta:
    """
    Represents the normalized net row-level change of a transaction.

    Responsibilities:
        - Normalize row operations
        - Enforce structural consistency
        - Provide logical resolution API

    Does NOT validate relational constraints.
    """

    # ============================================================
    # Init
    # ============================================================

    def __init__(
        self,
        engine: Any,
        operations: list[Any],
    ) -> None:
        self.engine = engine

        # node -> {id -> full row}
        self._inserted: defaultdict[
            Node,
            dict[str, dict[str, Any]],
        ] = defaultdict(dict)

        # node -> set[id]
        self._removed: defaultdict[
            Node,
            set[str],
        ] = defaultdict(set)

        # node -> {id -> {field -> value}}
        self._updated: defaultdict[
            Node,
            defaultdict[str, dict[str, Any]],
        ] = defaultdict(
            lambda: defaultdict(dict)
        )

        # metadata
        self._touched_nodes: set[Node] = set()

        self._touched_fields: defaultdict[
            Node,
            set[str],
        ] = defaultdict(set)

        for operation in operations:
            self._touched_nodes.add(
                operation.node,
            )

            if isinstance(operation, InsertOperation):
                self._insert(
                    operation.node,
                    operation.id,
                    operation.value,
                )

            elif isinstance(operation, RemoveOperation):
                self._remove(
                    operation.node,
                    operation.id,
                )

            elif isinstance(operation, SetRowOperation):
                self._update(
                    operation.node,
                    operation.id,
                    operation.key,
                    operation.new_value,
                )

    # ============================================================
    # Structural Rules
    # ============================================================

    def _insert(
        self,
        node: Node,
        row_id: str,
        row: dict[str, Any],
    ) -> None:
        # Insert after remove is treated as a replacement.
        if row_id in self._removed[node]:
            self._removed[node].remove(
                row_id,
            )

        # Duplicate insert in the same transaction.
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

        # Insert existing base ID, unless it was removed first.
        if (
            row_id in node.value
            and row_id not in self._removed[node]
        ):
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

        self._inserted[node][row_id] = dict(
            row,
        )

        for field in row:
            self._touched_fields[node].add(
                field,
            )

    def _remove(
        self,
        node: Node,
        row_id: str,
    ) -> None:
        # Removing a row inserted in this transaction cancels the insertion.
        if row_id in self._inserted[node]:
            del self._inserted[node][row_id]
            return

        # Duplicate remove.
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

        # Removing a nonexistent row.
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

        self._removed[node].add(
            row_id,
        )

        # Drop pending updates.
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
        self._touched_fields[node].add(
            field,
        )

        # Updating a row inserted in this transaction updates that row.
        if row_id in self._inserted[node]:
            self._inserted[node][row_id][field] = value
            return

        # Updating a row removed in this transaction is a conflict.
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

        # Updating a nonexistent row.
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

    # ============================================================
    # Logical Resolution API
    # ============================================================

    def exists(
        self,
        node: Node,
        row_id: str,
    ) -> bool:
        """
        Return whether a row logically exists after applying the delta.
        """
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
        """
        Resolve a logical field value after applying the delta.
        """
        if row_id in self._inserted[node]:
            return self._inserted[node][row_id].get(
                field,
            )

        if row_id in self._removed[node]:
            return None

        updated_fields = self._updated[node].get(
            row_id,
        )

        if (
            updated_fields
            and field in updated_fields
        ):
            return updated_fields[field]

        row = node.value.get(
            row_id,
        )

        return (
            row.get(field)
            if row
            else None
        )

    def ids(
        self,
        node: Node,
    ) -> set[str]:
        """
        Return the logical row IDs after applying the delta.
        """
        base_ids = set(
            node.value.keys(),
        )

        if self._removed[node]:
            base_ids -= self.removed_ids(
                node,
            )

        if self._inserted[node]:
            base_ids |= self.inserted_ids(
                node,
            )

        return base_ids

    def removed_ids(
        self,
        node: Node,
    ) -> set[str]:
        return set(
            self._removed[node],
        )

    def inserted_ids(
        self,
        node: Node,
    ) -> set[str]:
        return set(
            self._inserted[node].keys(),
        )

    def updated_ids(
        self,
        node: Node,
    ) -> set[str]:
        return set(
            self._updated[node].keys(),
        )

    # ============================================================
    # Metadata
    # ============================================================

    @property
    def touched_nodes(
        self,
    ) -> set[Node]:
        return self._touched_nodes

    def touched_fields(
        self,
        node: Node,
    ) -> set[str]:
        return self._touched_fields[node]
