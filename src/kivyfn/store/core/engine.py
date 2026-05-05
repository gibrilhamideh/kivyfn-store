from __future__ import annotations
from typing import TYPE_CHECKING

from kivyfn.store.core.operations import Operation
from kivyfn.store.address import Address

if TYPE_CHECKING:
    from kivyfn.store.node import Node
    from kivyfn.store.events import NodeEvent
    from kivyfn.store.builder import StoreConfig
    from kivyfn.store.transport import SocketIOServer


class Engine:
    def __init__(self, transport: SocketIOServer, root: Node, config: StoreConfig):
        self.transport = transport
        self.root = root
        self.config = config
        self._schema_nodes: dict[str, Node] = {}
        self._history: list[list[Operation]] = []
        self._redo: list[list[Operation]] = []

        # TODO: add node lookup not by iterating the address
        self._nodes: dict[Address, Node] = {}

        self._register_nodes(root)

    def _register_nodes(self, node: Node, current_path: tuple[str, ...] = ()):
        # 1. Map the current path to this node
        # The root will be mapped to an empty Address ()
        address = Address(*current_path)
        self._nodes[address] = node
        # node.address = address

        # 2. Schema registration logic
        name = node.schema.__name__
        if name in self._schema_nodes:
            raise RuntimeError(f"Schema '{name}' is attached to multiple nodes")
        self._schema_nodes[name] = node

        # 3. Recurse: Add the child's name to the path for the next level
        for child_name, child_node in node.children.items():
            # child_name is the key in the parent's children dict (e.g., "users")
            new_path = current_path + (child_name,)
            self._register_nodes(child_node, new_path)

    def resolve(self, address: Address) -> Node:
        return self._nodes[address]

    def get_node_by_schema(self, schema_name: str) -> Node:
        return self._schema_nodes[schema_name]

    def undo(self):
        pass

    def redo(self):
        pass

    def dispatch(self, event: NodeEvent) -> None:
        node = self.resolve(event.address)
        listeners = node.dispatch(event)
        self.transport.dispatch(event, listeners)

    def to_disk(self, nodes: set[Node]):
        storage = self.config.storage
        if storage is None:
            return

        for node in nodes:
            if node.schema.persistent:
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
