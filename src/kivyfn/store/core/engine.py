from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kivyfn.store.address import Address
from kivyfn.store.core.execution import Execution
from kivyfn.store.core.metadata import EngineMetadata
from kivyfn.store.events.manager import SubscriptionManager

if TYPE_CHECKING:
    from kivyfn.store.node import Node
    from kivyfn.store.builder import StoreConfig
    from kivyfn.store.transport import SocketIOServer
    from kivyfn.store.events.event import StoreEvent
    from kivyfn.store.core.operations import Operation
    from kivyfn.store.events.manager import DispatchMessage


class Engine:
    def __init__(
        self,
        transport: SocketIOServer,
        root: Node,
        config: StoreConfig,
    ) -> None:
        self.transport = transport
        self.config = config
        self.metadata = EngineMetadata.build(root)

        self.subscriptions = SubscriptionManager(self)

        self._history: list[list[Operation]] = []
        self._redo: list[list[Operation]] = []

    # -------------------------------------------------
    # Node lookup
    # -------------------------------------------------

    def get_node_by_address(self, address: Address) -> Node:
        return self.metadata.get_node_by_address(address)

    def get_node_by_schema(self, name: str) -> Node:
        return self.metadata.get_node_by_schema(name)

    def get_node_by_event(self, event: StoreEvent) -> Node:
        return self.metadata.get_node_by_address(event.address)

    # -------------------------------------------------
    # Read
    # -------------------------------------------------
    def snapshot(self, payload: dict[str, Any]) -> Any:
        address = Address(*payload["address"])
        node = self.get_node_by_address(address)

        return node.snapshot(
            id=payload.get("id"),
            key=payload.get("key"),
            resolve_foreignkeys=payload.get("resolve_foreignkeys", False),
        )

    # -------------------------------------------------
    # Subscriptions
    # -------------------------------------------------

    def subscribe(self, sid: str, payload: dict[str, Any]):
        return self.subscriptions.subscribe(sid, payload)

    def unsubscribe(self, sid: str, payload: dict[str, Any]):
        return self.subscriptions.unsubscribe(sid, payload["token"])

    def remove_sid(self, sid: str):
        return self.subscriptions.remove_sid(sid)

    def dispatch(self, event: StoreEvent) -> list[DispatchMessage]:
        return self.subscriptions.dispatch(event)

    # -------------------------------------------------
    # Execute
    # -------------------------------------------------
    def execute(self, commands: list) -> tuple[Any, list[DispatchMessage]]:
        execution = Execution(self, commands).execute()

        self.to_disk(execution.touched_nodes)

        messages: list[DispatchMessage] = []
        for event in execution.events:
            messages.extend(self.dispatch(event))

        return execution, messages

    # -------------------------------------------------
    # Undo / Redo
    # -------------------------------------------------

    def undo(self):
        pass

    def redo(self):
        pass

    # -------------------------------------------------
    # Persistence
    # -------------------------------------------------

    def to_disk(self, nodes: set[Node]) -> None:
        storage = self.config.storage
        if storage is None:
            return

        for node in nodes:
            if self.metadata.is_persistent(node):
                storage.write(node.schema.name, node.value)

    def boot(self) -> None:
        storage = self.config.storage
        if storage is None:
            return

        for node in self.metadata.persistent_nodes:
            data = storage.read(node.schema.name)
            node.build(data)
