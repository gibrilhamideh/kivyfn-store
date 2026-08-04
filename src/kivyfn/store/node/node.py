# kivyfn/store/node/node.py
from __future__ import annotations

import copy

from typing import TYPE_CHECKING, Any, Literal, overload

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
from kivyfn.store.error.detail import StoreAddress
from kivyfn.store.node.index import IndexManager


if TYPE_CHECKING:
    from kivyfn.store.schema import Schema


logger = get_logger(__name__)


class Node:
    def __init__(self, schema: Schema, address: Address) -> None:
        self.schema = schema
        self.address = address

        self.name: str | int | None = None
        self.parent: Node | None = None
        self.children: dict[str, Node] = {}

        self.value: dict | dict[str, dict] = {}
        self._indexes = IndexManager(schema)

    def __repr__(self) -> str:
        return (
            f"Node("
            f"schema={self.schema.name}, "
            f"children={len(self.children)}"
            f")"
        )

    # ---------------------------
    # Build / boot
    # ---------------------------

    def build(self, data: Any, *, recover: bool = False) -> None:
        if self.schema.many:
            self._build_many(data, recover=recover)
        else:
            self._build_single(data, recover=recover)

    def _build_many(self, data: Any, *, recover: bool) -> None:
        self.value = {}
        self._indexes = IndexManager(self.schema)

        if data is None:
            return

        if not isinstance(data, dict):
            if not recover:
                raise TypeError(
                    f"Persisted schema {self.schema.name!r} "
                    "must contain a dictionary"
                )

            logger.error(
                "Resetting persisted schema %s: expected dictionary, got %s",
                self.schema.name,
                type(data).__name__,
            )
            return

        for raw_row_id, row in data.items():
            row_id = str(raw_row_id)

            if not isinstance(row, dict):
                if not recover:
                    raise TypeError(
                        f"Persisted row {self.schema.name}[{row_id}] "
                        "must contain a dictionary"
                    )

                logger.error(
                    "Removing persisted row %s[%s]: "
                    "expected dictionary, got %s",
                    self.schema.name,
                    row_id,
                    type(row).__name__,
                )
                continue

            built = self._build_default(
                row,
                recover=recover,
                row_id=row_id,
            )

            try:
                self._insert_built(row_id, built)
            except (ConstraintError, RowAlreadyExistsError) as error:
                if not recover:
                    raise

                logger.error(
                    "Removing persisted row %s[%s]: %s",
                    self.schema.name,
                    row_id,
                    error,
                )

    def _build_single(self, data: Any, *, recover: bool) -> None:
        if data is None:
            data = {}
        elif not isinstance(data, dict):
            if not recover:
                raise TypeError(
                    f"Persisted schema {self.schema.name!r} "
                    "must contain a dictionary"
                )

            logger.error(
                "Resetting persisted schema %s: "
                "expected dictionary, got %s",
                self.schema.name,
                type(data).__name__,
            )

            data = {}

        self.value = self._build_default(
            data,
            recover=recover,
            row_id=None,
        )

    def _build_default(
        self,
        row: dict,
        *,
        recover: bool = False,
        row_id: str | None = None,
    ) -> dict:
        built: dict[str, Any] = {}

        for name, field in self.schema.fields().items():
            default = getattr(field, "default", None)
            value = row.get(name, default)

            try:
                self.schema.challenge_field(name, value)
            except FieldValidationError as error:
                if not recover:
                    raise

                self.schema.challenge_field(name, default)

                location = self.schema.name

                if row_id is not None:
                    location += f"[{row_id}]"

                logger.error(
                    "Recovering persisted field %s.%s: "
                    "invalid value %r replaced with default %r: %s",
                    location,
                    name,
                    value,
                    default,
                    error,
                )

                value = default

            built[name] = value

        return built

    def _insert_built(self, row_id: str, row: dict) -> None:
        if row_id in self.value:
            raise RowAlreadyExistsError(
                f"Row {row_id!r} already exists",
                detail=RowErrorDetail(
                    schema=self.schema.name,
                    address=self._address(),
                    row_id=row_id,
                    operation="insert",
                ),
            )

        self._indexes.add(row_id, row)
        self.value[row_id] = row

    # ---------------------------
    # Tree
    # ---------------------------

    def add_child(self, name: str, node: Node) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError("Child name must be a non-empty string")

        if not isinstance(node, Node):
            raise TypeError("add_child() requires a Node instance")

        if name in self.children:
            raise ValueError(f"Child {name!r} already exists")

        node.parent = self
        node.name = name
        self.children[name] = node

    # ---------------------------
    # Data API used by operations
    # ---------------------------

    def insert(self, row_id: str, row: dict) -> None:
        if not self.schema.many:
            raise InvalidOperationError(
                "insert() is only valid for many schemas",
                detail=RowErrorDetail(
                    schema=self.schema.name,
                    address=self._address(),
                    row_id=row_id,
                    operation="insert",
                ),
            )

        built = self._build_default(row)
        self._insert_built(row_id, built)

    def setrow(self, row_id: str, key: str, value: Any) -> Any:
        if not self.schema.many:
            raise InvalidOperationError(
                "setrow() is only valid for many schemas",
                detail=RowErrorDetail(
                    schema=self.schema.name,
                    address=self._address(),
                    row_id=row_id,
                    operation="setrow",
                ),
            )

        if row_id not in self.value:
            raise RowNotFoundError(
                f"Row {row_id!r} does not exist",
                detail=RowErrorDetail(
                    schema=self.schema.name,
                    address=self._address(),
                    row_id=row_id,
                    operation="setrow",
                ),
            )

        old_row = dict(self.value[row_id])
        old_value = old_row.get(key)

        self.schema.challenge_field(key, value)

        new_row = dict(old_row)
        new_row[key] = value

        self._indexes.update(row_id, old_row, new_row)
        self.value[row_id] = new_row

        return old_value

    def remove(self, row_id: str) -> dict:
        if not self.schema.many:
            raise InvalidOperationError(
                "remove() is only valid for many schemas",
                detail=RowErrorDetail(
                    schema=self.schema.name,
                    address=self._address(),
                    row_id=row_id,
                    operation="remove",
                ),
            )

        if row_id not in self.value:
            raise RowNotFoundError(
                f"Row {row_id!r} does not exist",
                detail=RowErrorDetail(
                    schema=self.schema.name,
                    address=self._address(),
                    row_id=row_id,
                    operation="remove",
                ),
            )

        old_row = dict(self.value[row_id])

        self._indexes.remove(row_id, old_row)
        del self.value[row_id]

        return old_row

    def set(self, key: str, value: Any) -> Any:
        if self.schema.many:
            raise InvalidOperationError(
                "set() is only valid for single schemas",
                detail=RowErrorDetail(
                    schema=self.schema.name,
                    address=self._address(),
                    operation="set",
                ),
            )

        self.schema.challenge_field(key, value)

        old_value = self.value.get(key)
        self.value[key] = value

        return old_value

    # ---------------------------
    # Read / lookup API
    # ---------------------------

    @overload
    def lookup(
        self,
        fields: str | tuple[str, ...],
        value: Any,
        *,
        first: Literal[True],
    ) -> str | None:
        ...

    @overload
    def lookup(
        self,
        fields: str | tuple[str, ...],
        value: Any,
        *,
        first: Literal[False] = False,
    ) -> set[str]:
        ...

    def lookup(
        self,
        fields: str | tuple[str, ...],
        value: Any,
        *,
        first: bool = False,
    ) -> set[str] | str | None:
        return self._indexes.lookup(fields, value, first=first)

    def exists(self, row_id: str) -> bool:
        if not self.schema.many:
            raise InvalidOperationError(
                "exists() is only valid for many schemas",
                detail=RowErrorDetail(
                    schema=self.schema.name,
                    address=self._address(),
                    row_id=row_id,
                    operation="exists",
                ),
            )

        return row_id in self.value

    def snapshot(
        self,
        id: str | None = None,
        key: str | None = None,
    ) -> Any:
        if self.schema.many:
            if id is None:
                return copy.deepcopy(self.value)

            if id not in self.value:
                return None

            row = self.value[id]

            if key is None:
                return copy.deepcopy(row)

            return row.get(key)

        if key is None:
            return copy.deepcopy(self.value)

        return self.value.get(key)

    # ---------------------------
    # Internal helpers
    # ---------------------------

    def _address(self) -> StoreAddress:
        return self.address.parts



