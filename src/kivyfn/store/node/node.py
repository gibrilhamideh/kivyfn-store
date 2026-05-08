from __future__ import annotations

from typing import TYPE_CHECKING, Any
import copy


from kivyfn.store.node.index import IndexManager

from kivyfn.store.address import Address
from kivyfn.store.errors import NodeError


if TYPE_CHECKING:
    from kivyfn.store.schema import Schema

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
        return f"Node(schema={self.schema.name}, children={len(self.children)})"

    # ---------------------------
    # Build / boot
    # ---------------------------
    def build(self, data: dict | None) -> None:
        if self.schema.many:
            self._build_many(data)
        else:
            self._build_single(data)

    def _build_many(self, data: dict | None) -> None:
        self.value = {}
        self._indexes = IndexManager(self.schema)

        if not isinstance(data, dict):
            return

        for row_id, row in data.items():
            if not isinstance(row, dict):
                continue

            try:
                prepared = self._build_default(row)
                self.insert(str(row_id), prepared)
            except Exception:
                # corrupted row / invalid field / index conflict during boot
                continue

    def _build_single(self, data: dict | None) -> None:
        if not isinstance(data, dict):
            data = {}

        self.value = self._build_default(data)

    def _build_default(self, row: dict) -> dict:
        """
        Build a complete row from schema defaults + provided data.

        No casting.
        No normalization.
        Only:
        - fill missing fields with defaults
        - validate final values
        """

        built = {}
        for name, field in self.schema.fields().items():
            if name in row:
                value = row[name]
            else:
                value = getattr(field, "default", None)

            self.schema.challenge_field(name, value)
            built[name] = value

        return built

    # ---------------------------
    # Tree
    # ---------------------------
    def add_child(self, name: str, node: Node) -> None:
        if name in self.children:
            raise NodeError(f"Child '{name}' already exists")

        node.parent = self
        node.name = name
        self.children[name] = node

    # ---------------------------
    # Data API used by operations
    # ---------------------------
    def insert(self, row_id: str, row: dict) -> None:
        if not self.schema.many:
            raise NodeError("insert() is only valid for many schemas")

        if row_id in self.value:
            raise NodeError(f"Row '{row_id}' already exists")

        built = self._build_default(row)

        self._indexes.add(row_id, built)
        self.value[row_id] = built

    def setrow(self, row_id: str, key: str, value: Any) -> Any:
        if not self.schema.many:
            raise NodeError("set_row() is only valid for many schemas")

        if row_id not in self.value:
            raise NodeError(f"Row '{row_id}' does not exist")

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
            raise NodeError("remove() is only valid for many schemas")

        if row_id not in self.value:
            raise NodeError(f"Row '{row_id}' does not exist")

        old_row = dict(self.value[row_id])

        self._indexes.remove(row_id, old_row)
        del self.value[row_id]

        return old_row

    def set(self, key: str, value: Any) -> Any:
        if self.schema.many:
            raise NodeError("set() is only valid for single schemas")

        self.schema.challenge_field(key, value)

        old_value = self.value.get(key)
        self.value[key] = value

        return old_value

    # ---------------------------
    # Read / lookup API
    # ---------------------------
    def exists(self, row_id: str) -> bool:
        if not self.schema.many:
            raise NodeError("exists() is only valid for many schemas")

        return row_id in self.value

    def lookup(
        self,
        fields: str | tuple[str, ...],
        value: Any,
        *,
        first: bool = False,
    ) -> set[str] | str | None:
        return self._indexes.lookup(fields, value, first=first)

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

        else:
            if key is None:
                return self.value

            return self.value.get(key)


