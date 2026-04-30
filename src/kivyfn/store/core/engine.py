from __future__ import annotations
from typing import TYPE_CHECKING

from .operations import Operation
from ..address import Address

if TYPE_CHECKING:
    from ..node import Node
    from ..events.event import NodeEvent
    from ..builder.config import StoreConfig


class Engine:
    def __init__(self, root: Node, config: StoreConfig):
        self.root = root
        self.config = config
        self._schema_nodes: dict[str, Node] = {}
        self._history: list[list[Operation]] = []
        self._redo: list[list[Operation]] = []

        self._register_nodes(root)

    def _register_nodes(self, node: Node):
        name = node.schema.__name__

        if name in self._schema_nodes:
            raise RuntimeError(f"Schema '{name}' is attached to multiple nodes")

        self._schema_nodes[name] = node

        for child in node.children.values():
            self._register_nodes(child)

    def resolve(self, address: Address) -> Node:
        node = self.root
        for part in address:
            node = node.children[part]
        return node

    def get_node_by_schema(self, schema_name: str) -> Node:
        return self._schema_nodes[schema_name]

    def undo(self):
        pass

    def redo(self):
        pass

    def dispatch(self, event: NodeEvent) -> None:
        node = self.resolve(event.address)
        node.dispatch(event)

    def to_disk(self, nodes: set[Node]):
        storage = self.config.storage
        if storage is None:
            return

        for node in nodes:
            if node.schema.persistant:
                name = node.schema.__name__
                data = node.value
                storage.write(name, data)

    def boot(self):
        storage = self.config.storage
        if storage is None:
            return

        for name, node in self._schema_nodes.items():
            data = storage.read(name)
            node.build(data)
