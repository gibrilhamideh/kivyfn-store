from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, ClassVar
import copy


from kivyfn.store.node.index import IndexManager

from kivyfn.store.constants import InstructionOpCode
from kivyfn.store.address import Address
from kivyfn.store.errors import NodeError
from kivyfn.store.events.event import NodeEvent
from kivyfn.store.events.listener import Listener, ForeignListener, ListenerIndex
from kivyfn.store.events.subscription import EngineSubscription

if TYPE_CHECKING:
    from kivyfn.store.schema import Schema


class Node:
    registry: ClassVar[dict[str, Node]] = {}

    def __init__(self, schema: type[Schema]) -> None:
        if schema.__name__ in Node.registry:
            raise NodeError(f"Duplicate schema name: {schema.__name__}")

        Node.registry[schema.__name__] = self
        self.schema = schema
        self.name: str | int | None = None
        self.parent: Node | None = None
        self.children: dict[str, Node] = {}

        # many=True  -> {row_id: row}
        # many=False -> {field: value}
        self.value: dict | dict[str, dict] = {}

        self._indexes = IndexManager(schema)
        self._listener_index: ListenerIndex = ListenerIndex()
        self._foreignkey_listeners: dict[str, list[ForeignListener]] = {}
        self._foreignkey_subscriptions: dict[str, list[EngineSubscription]] = {}

    def __repr__(self) -> str:
        return f"Node(schema={self.schema.__name__}, children={len(self.children)})"

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
        return row

    # ---------------------------
    # Tree
    # ---------------------------
    def add_child(self, name: str, node: Node) -> None:
        if name in self.children:
            raise NodeError(f"Child '{name}' already exists")

        node.parent = self
        node.name = name
        self.children[name] = node

    def address(self) -> Address:
        parts = []
        node = self

        while node.parent is not None:
            parts.append(node.name)
            node = node.parent

        parts.reverse()
        return Address(*parts)

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
        *,
        resolve_foreignkeys: bool = False,
    ) -> Any:
        if self.schema.many:
            # Full table
            if id is None:
                if not resolve_foreignkeys:
                    return copy.deepcopy(self.value)

                return {
                    row_id: self._resolve_foreignkeys(row)
                    for row_id, row in self.value.items()
                }

            # Row-level
            if id not in self.value:
                return None

            row = self.value[id]

            if key is None:
                if not resolve_foreignkeys:
                    return copy.deepcopy(row)

                return self._resolve_foreignkeys(row)

            # Field-level
            if key not in row:
                return None

            return row[key]

        else:
            # Single schema
            if key is None:
                if not resolve_foreignkeys:
                    return self.value

                return self._resolve_foreignkeys(self.value)

            if key not in self.value:
                return None

            return self.value[key]

    def _resolve_foreignkeys(self, row: dict) -> dict:
        resolved = dict(row)

        for fk in self.schema.foreign_key_fields():
            fk_id = row.get(fk)
            if fk_id is None:
                continue

            field = self.schema.get_field(fk)
            ref_node = Node.registry.get(field.schema)  # pyright: ignore

            if ref_node is None:
                continue

            ref_row = ref_node.snapshot(
                id=fk_id,
                resolve_foreignkeys=True,
            )

            if not ref_row:
                continue

            resolved.update(ref_row)

        return resolved

    # ---------------------------
    # Subscriptions
    # ---------------------------
    def subscribe(
        self,
        opcode: InstructionOpCode,
        id: str | None = None,
        key: str | None = None,
        depth: int | None = 0,
        resolve_foreignkeys: bool = False,
    ) -> Listener:
        listener = Listener(
            address=self.address(),
            opcode=opcode,
            id=id,
            key=key,
            resolve_foreignkeys=resolve_foreignkeys,
            depth=depth,
        )

        self._listener_index.add(listener)
        return listener

    def unsubscribe(self, listener: Listener) -> None:
        # remove local listener
        self._listener_index.remove(listener)

        # remove foreign subscriptions
        # subs = self._foreignkey_subscriptions.pop(token, None)
        # if subs:
        #     for sub in subs:
        #         sub.dispose()

        # remove foreign listeners
        # self._foreignkey_listeners.pop(token, None)

    def dispatch(self, event: NodeEvent) -> list[Listener]:
        deliveries = []

        for listener in self._listener_index.listeners(event):
            deliveries.append(listener)

        return deliveries

    def _subscribe_foreignkeys(
        self,
        token: str,
        callback: Callable,
        opcode: InstructionOpCode | None,
        id: str | None,
        key: str | None,
        depth: int | None,
    ):
        if opcode != InstructionOpCode.SETROW:
            return

        if depth == 0:
            return

        if depth is not None and depth < 0:
            return

        next_depth = None if depth is None else depth - 1

        foreignkey_fields = self.schema.foreign_key_fields()

        if key is not None:
            if key in foreignkey_fields:
                self._subscribe_foreignkey(
                    token,
                    callback,
                    id=id,
                    foreignkey=key,
                    depth=next_depth,
                )
            return

        for foreignkey in foreignkey_fields:
            self._subscribe_foreignkey(
                token,
                callback,
                id=id,
                foreignkey=foreignkey,
                depth=next_depth,
            )

    def _subscribe_foreignkey(
        self,
        token: str,
        callback: Callable,
        *,
        id: str | None,
        foreignkey: str,
        depth: int | None,
    ):
        return
        # # 1. Resolve the referenced node
        # field = self.schema.get_field(foreignkey)
        # referenced_schema_name = field.schema  # pyright: ignore
        # referenced_node = Node.registry.get(referenced_schema_name)
        #
        # if not referenced_node:
        #     raise NodeError(
        #         f"Referenced node '{referenced_schema_name}' not found in registry."
        #     )
        #
        # # 2. Create the Adapter (The ForeignListener)
        # # We pass the original UI callback here. The ForeignListener
        # # will handle the weakref to the UI internally.
        # adapter = ForeignListener(
        #     owner=self, foreignkey=foreignkey, callback=callback, subscriber_id=id
        # )
        #
        # # 3. Subscribe to the foreign node
        # # We store the adapter in _foreignkey_listeners,
        # # so the weak Listener inside referenced_node does not lose it.
        # subscription = referenced_node.subscribe(
        #     adapter,
        #     opcode=InstructionOpCode.SETROW,
        #     id=None,
        #     key=None,
        #     depth=depth,
        # )
        #
        # # 4. Store the subscription handle for cleanup during unsubscribe()
        # self._foreignkey_listeners.setdefault(token, []).append(adapter)
        # self._foreignkey_subscriptions.setdefault(token, []).append(subscription)
