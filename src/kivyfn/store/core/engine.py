from __future__ import annotations
from typing import TYPE_CHECKING

from kivyfn.store.address import Address
from kivyfn.store.core.metadata import EngineMetadata

if TYPE_CHECKING:
    from kivyfn.store.node import Node
    from kivyfn.store.builder import StoreConfig
    from kivyfn.store.transport import SocketIOServer
    from kivyfn.store.core.operations import Operation


class Engine:
    def __init__(self, transport: SocketIOServer, root: Node, config: StoreConfig):
        self.transport = transport
        self.config = config
        self.metadata = EngineMetadata.build(root)


        self._history: list[list[Operation]] = []
        self._redo: list[list[Operation]] = []

    def resolve(self, address: Address) -> Node:
        return self.metadata.resolve(address)

    def get_node_by_schema(self, name: str) -> Node:
        return self.metadata.get_node_by_schema(name)

    def undo(self):
        pass

    def redo(self):
        pass

    def to_disk(self, nodes: set[Node]):
        storage = self.config.storage
        if storage is None:
            return

        for node in nodes:
            if self.metadata.is_persistent(node):
                storage.write(node.schema.name, node.value)

    def boot(self):
        storage = self.config.storage
        if storage is None:
            return

        for node in self.metadata.persistent_nodes:
            data = storage.read(node.schema.name)
            node.build(data)
