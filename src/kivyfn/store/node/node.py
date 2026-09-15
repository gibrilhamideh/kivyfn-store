# src/kivyfn/store/node/node.py
from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from kivyfn.logging import get_logger

from kivyfn.store.address import Address
from kivyfn.store.error import (
    ConstraintError,
    FieldValidationError,
    InvalidOperationError,
    RowAlreadyExistsError,
    RowErrorDetail,
    RowNotFoundError,
)
from kivyfn.store.node.index import IndexManager
from kivyfn.store.schema import ManySchema, Schema, SingleSchema

logger = get_logger(__name__)

Row = dict[str, Any]


@dataclass(slots=True)
class NodeState:
    """A validated, privately owned value and its final indexes."""

    value: dict[str, Any]
    indexes: IndexManager | None


class Node:
    """Internal canonical data for one Store schema.

    Store callers should use snapshots and transactions. Direct Node mutation
    bypasses field validation, relational constraints, persistence, revisions
    and commit delivery; it is reserved for Engine and boot implementation.
    """

    def __init__(self, schema: Schema, address: Address) -> None:
        if not isinstance(schema, (SingleSchema, ManySchema)):
            raise TypeError(
                f"Schema {type(schema).__name__!r} must inherit "
                "SingleSchema or ManySchema"
            )

        self.schema = schema
        self.address = address
        self.children: dict[str, Node] = {}

        self.value: dict[str, Any] = {}

        self._revision = 0
        self._indexes = (
            IndexManager(schema) if isinstance(schema, ManySchema) else None
        )

    def __repr__(self) -> str:
        return (
            f"Node(schema={self.schema.name}, children={len(self.children)})"
        )

    # -------------------------------------------------
    # Schema revision
    # -------------------------------------------------

    @property
    def revision(self) -> int:
        """Current schema revision within this Store instance."""
        return self._revision

    def advance_revision(self) -> int:
        """Advance once after a successful commit changes this schema.

        Store calls this under its mutation lock after execution succeeds.
        Boot and low-level mutation methods do not advance the counter.
        The counter is in memory only and has no retained event history.
        """
        self._revision += 1
        return self._revision

    # -------------------------------------------------
    # Build / boot
    # -------------------------------------------------

    def build(self, data: Any, *, recover: bool = False) -> None:
        if isinstance(self.schema, ManySchema):
            self._build_many(data, recover=recover)
            return

        self._build_single(data, recover=recover)

    def _build_many(self, data: Any, *, recover: bool) -> None:
        self.value = {}
        self._indexes = IndexManager(self.schema)

        if data is None:
            return

        if not isinstance(data, Mapping):
            if not recover:
                raise TypeError(
                    f"Persisted schema {self.schema.name!r} "
                    "must contain a mapping"
                )

            logger.error(
                "Resetting persisted schema %s: expected mapping, got %s",
                self.schema.name,
                type(data).__name__,
            )
            return

        for raw_row_id, raw_row in data.items():
            row_id = str(raw_row_id)

            if not isinstance(raw_row, Mapping):
                if not recover:
                    raise TypeError(
                        f"Persisted row {self.schema.name}[{row_id}] "
                        "must contain a mapping"
                    )

                logger.error(
                    "Removing persisted row %s[%s]: expected mapping, got %s",
                    self.schema.name,
                    row_id,
                    type(raw_row).__name__,
                )
                continue

            try:
                row = self.build_row(raw_row, recover=recover, row_id=row_id)
            except FieldValidationError as exc:
                if not recover:
                    raise

                logger.error(
                    "Removing persisted row %s[%s]: "
                    "field could not be recovered: %s",
                    self.schema.name,
                    row_id,
                    exc,
                )
                continue

            try:
                self.insert(row_id, row)
            except (ConstraintError, RowAlreadyExistsError) as exc:
                if not recover:
                    raise

                logger.error(
                    "Removing persisted row %s[%s]: %s",
                    self.schema.name,
                    row_id,
                    exc,
                )

    def _build_single(self, data: Any, *, recover: bool) -> None:
        if data is None:
            data = {}

        elif not isinstance(data, Mapping):
            if not recover:
                raise TypeError(
                    f"Persisted schema {self.schema.name!r} "
                    "must contain a mapping"
                )

            logger.error(
                "Resetting persisted schema %s: expected mapping, got %s",
                self.schema.name,
                type(data).__name__,
            )

            data = {}

        self.value = self.build_row(data, recover=recover)

    def build_row(
        self,
        values: Mapping[str, Any],
        *,
        recover: bool = False,
        row_id: str | None = None,
    ) -> Row:
        """Resolve defaults and validate one schema value."""

        row: Row = {}

        for name, field in self.schema.fields().items():
            default = getattr(field, "default", None)
            value = values.get(name, default)

            try:
                self.schema.challenge_field(name, value)
            except FieldValidationError as exc:
                if not recover:
                    raise

                self.schema.challenge_field(name, default)

                location = self.schema.name

                if row_id is not None:
                    location += f"[{row_id}]"

                logger.error(
                    "Recovering persisted field %s.%s: invalid value %r "
                    "replaced with default %r: %s",
                    location,
                    name,
                    value,
                    default,
                    exc,
                )

                value = default

            row[name] = copy.deepcopy(value)

        return row

    # -------------------------------------------------
    # Tree
    # -------------------------------------------------

    def add_child(self, name: str, node: Node) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError("Child name must be a non-empty string")

        if not isinstance(node, Node):
            raise TypeError("add_child() requires a Node instance")

        if name in self.children:
            raise ValueError(f"Child {name!r} already exists")

        self.children[name] = node

    def prepare(self, data: dict[str, Any]) -> NodeState:
        """Build final indexes before publishing privately owned data.

        The caller transfers ownership of data. This is an Engine operation;
        field and row structure validation has already happened in Compiler.
        """
        indexes = None
        if isinstance(self.schema, ManySchema):
            indexes = IndexManager(self.schema)
            for row_id, row in data.items():
                indexes.add(row_id, row)
        return NodeState(data, indexes)

    def install(self, state: NodeState) -> None:
        """Publish prepared state without validation or application code."""
        self.value = state.value
        self._indexes = state.indexes

    # -------------------------------------------------
    # Canonical mutation API
    # -------------------------------------------------

    def insert(self, row_id: str, row: Mapping[str, Any]) -> None:
        indexes = self._many_indexes(operation="insert", row_id=row_id)

        if row_id in self.value:
            raise RowAlreadyExistsError(
                f"Row {row_id!r} already exists",
                detail=RowErrorDetail(
                    schema=self.schema.name,
                    address=self.address.parts,
                    row_id=row_id,
                    operation="insert",
                ),
            )

        stored = copy.deepcopy(dict(row))

        indexes.add(row_id, stored)

        self.value[row_id] = stored

    def setrow(self, row_id: str, key: str, value: Any) -> Any:
        indexes = self._many_indexes(operation="setrow", row_id=row_id)

        row = self.value.get(row_id)

        if not isinstance(row, dict):
            raise RowNotFoundError(
                f"Row {row_id!r} does not exist",
                detail=RowErrorDetail(
                    schema=self.schema.name,
                    address=self.address.parts,
                    row_id=row_id,
                    operation="setrow",
                ),
            )

        old_row = dict(row)
        old_value = old_row.get(key)

        new_row = dict(old_row)
        new_row[key] = copy.deepcopy(value)

        indexes.update(row_id, old_row, new_row)

        self.value[row_id] = new_row

        return copy.deepcopy(old_value)

    def remove(self, row_id: str) -> Row:
        indexes = self._many_indexes(operation="remove", row_id=row_id)

        row = self.value.get(row_id)

        if not isinstance(row, dict):
            raise RowNotFoundError(
                f"Row {row_id!r} does not exist",
                detail=RowErrorDetail(
                    schema=self.schema.name,
                    address=self.address.parts,
                    row_id=row_id,
                    operation="remove",
                ),
            )

        old_row = dict(row)

        indexes.remove(row_id, old_row)

        del self.value[row_id]

        return copy.deepcopy(old_row)

    def set(self, key: str, value: Any) -> Any:
        self._require_single(operation="set")

        old_value = self.value.get(key)
        self.value[key] = copy.deepcopy(value)

        return copy.deepcopy(old_value)

    # -------------------------------------------------
    # Read / lookup API
    # -------------------------------------------------

    def lookup_one(
        self, fields: str | tuple[str, ...], value: Any
    ) -> str | None:
        indexes = self._many_indexes(operation="lookup")

        return indexes.lookup_one(fields, value)

    def lookup_all(
        self, fields: str | tuple[str, ...], value: Any
    ) -> frozenset[str]:
        indexes = self._many_indexes(operation="lookup")

        return indexes.lookup_all(fields, value)

    def exists(self, row_id: str) -> bool:
        self._many_indexes(operation="exists", row_id=row_id)

        return row_id in self.value

    def snapshot(self, id: str | None = None, key: str | None = None) -> Any:
        if isinstance(self.schema, ManySchema):
            if id is None:
                return copy.deepcopy(self.value)

            row = self.value.get(id)

            if not isinstance(row, dict):
                return None

            if key is None:
                return copy.deepcopy(row)

            return copy.deepcopy(row.get(key))

        if key is None:
            return copy.deepcopy(self.value)

        return copy.deepcopy(self.value.get(key))

    # -------------------------------------------------
    # Internal helpers
    # -------------------------------------------------

    def _many_indexes(
        self, *, operation: str, row_id: str | None = None
    ) -> IndexManager:
        if not isinstance(self.schema, ManySchema) or self._indexes is None:
            raise InvalidOperationError(
                f"{operation}() is only valid for many schemas",
                detail=RowErrorDetail(
                    schema=self.schema.name,
                    address=self.address.parts,
                    row_id=row_id,
                    operation=operation,
                ),
            )

        return self._indexes

    def _require_single(self, *, operation: str) -> None:
        if isinstance(self.schema, SingleSchema):
            return

        raise InvalidOperationError(
            f"{operation}() is only valid for single schemas",
            detail=RowErrorDetail(
                schema=self.schema.name,
                address=self.address.parts,
                operation=operation,
            ),
        )
