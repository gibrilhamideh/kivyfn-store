# kivyfn/store/core/engine.py
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kivyfn.store.address import Address
from kivyfn.store.core.boot import StoreBoot
from kivyfn.store.core.execution import Execution
from kivyfn.store.core.metadata import EngineMetadata
from kivyfn.store.events.manager import SubscriptionManager
from kivyfn.store.signals.manager import SignalManager
from kivyfn.store.storage import FileStorage


if TYPE_CHECKING:
    from kivyfn.store.builder import StoreConfig
    from kivyfn.store.core.operations import Operation
    from kivyfn.store.events.event import StoreEvent
    from kivyfn.store.events.manager import DispatchMessage
    from kivyfn.store.node import Node
    from kivyfn.store.signals.manager import (
        BindSignalResult,
        SignalDispatchMessage,
        UnbindSignalResult,
    )
    from kivyfn.store.transport import SocketIOServer


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
        self.signals = SignalManager(self)

        self._storage = FileStorage(
            self.config.storage_path,
        )

        self._history: list[list[Operation]] = []
        self._redo: list[list[Operation]] = []

    # -------------------------------------------------
    # Node lookup
    # -------------------------------------------------

    def get_node_by_address(
        self,
        address: Address[Any],
    ) -> Node:
        return self.metadata.get_node_by_address(
            address,
        )

    def get_node_by_schema(
        self,
        name: str,
    ) -> Node:
        return self.metadata.get_node_by_schema(
            name,
        )

    def get_node_by_event(
        self,
        event: StoreEvent,
    ) -> Node:
        return self.metadata.get_node_by_address(
            event.address,
        )

    # -------------------------------------------------
    # Read
    # -------------------------------------------------

    def lookup(
        self,
        payload: dict[str, Any],
    ) -> Any:
        address = Address(*payload["address"])
        node = self.get_node_by_address(address)

        fields = tuple(payload["fields"])

        if payload["first"]:
            row_id = node.lookup(
                fields=fields,
                value=payload["value"],
                first=True,
            )

            if row_id is None:
                return None

            return node.snapshot(
                id=row_id,
            )

        row_ids = node.lookup(
            fields=fields,
            value=payload["value"],
            first=False,
        )

        return {
            row_id: node.snapshot(id=row_id)
            for row_id in row_ids
        }


    def snapshot(
        self,
        payload: dict[str, Any],
    ) -> Any:
        address = Address(
            *payload["address"],
        )

        node = self.get_node_by_address(
            address,
        )

        data = node.snapshot(
            id=payload.get("id"),
            key=payload.get("key"),
        )

        if not payload.get(
            "resolve_foreignkeys",
            False,
        ):
            return data

        if data is None or payload.get("key") is not None:
            return data

        if node.schema.many and payload.get("id") is None:
            return {
                row_id: self.resolve_foreignkeys(
                    node,
                    row,
                )
                for row_id, row in data.items()
            }

        return self.resolve_foreignkeys(
            node,
            data,
        )

    def resolve_foreignkeys(
        self,
        node: Node,
        row: dict[str, Any],
    ) -> dict[str, Any]:
        resolved = dict(row)

        for foreignkey in node.schema.foreign_key_fields():
            foreign_id = row.get(
                foreignkey,
            )

            if foreign_id is None:
                continue

            field = node.schema.get_field(
                foreignkey,
            )

            referenced_node = self.get_node_by_schema(
                field.schema,  # pyright: ignore[reportAttributeAccessIssue]
            )

            referenced_row = referenced_node.snapshot(
                id=foreign_id,
            )

            if referenced_row is None:
                continue

            resolved.update(
                self.resolve_foreignkeys(
                    referenced_node,
                    referenced_row,
                )
            )

        return resolved

    # -------------------------------------------------
    # Subscriptions
    # -------------------------------------------------

    def subscribe(
        self,
        sid: str,
        payload: dict[str, Any],
    ):
        return self.subscriptions.subscribe(
            sid,
            payload,
        )

    def unsubscribe(
        self,
        sid: str,
        payload: dict[str, Any],
    ):
        return self.subscriptions.unsubscribe(
            sid,
            payload["token"],
        )

    def dispatch(
        self,
        event: StoreEvent,
    ) -> list[DispatchMessage]:
        return self.subscriptions.dispatch(
            event,
        )

    # -------------------------------------------------
    # Signals
    # -------------------------------------------------

    def bind_signal(
        self,
        sid: str,
        payload: dict[str, Any],
    ) -> BindSignalResult:
        return self.signals.bind(
            sid,
            payload,
        )

    def unbind_signal(
        self,
        sid: str,
        payload: dict[str, Any],
    ) -> UnbindSignalResult:
        return self.signals.unbind(
            sid,
            payload["token"],
        )

    def emit_signal(
        self,
        payload: dict[str, Any],
    ) -> list[SignalDispatchMessage]:
        return self.signals.emit(
            payload,
        )

    # -------------------------------------------------
    # Client lifecycle
    # -------------------------------------------------

    def remove_sid(
        self,
        sid: str,
    ) -> None:
        self.subscriptions.remove_sid(
            sid,
        )

        self.signals.remove_sid(
            sid,
        )

    # -------------------------------------------------
    # Execute
    # -------------------------------------------------

    def execute(
        self,
        commands: list[dict[str, Any]],
    ) -> tuple[Any, list[DispatchMessage]]:
        execution = Execution(
            self,
            commands,
        ).execute()

        self.to_disk(
            execution.touched_nodes,
        )

        messages: list[DispatchMessage] = []

        for event in execution.events:
            messages.extend(
                self.dispatch(
                    event,
                )
            )

        return execution, messages

    # -------------------------------------------------
    # Undo / Redo
    # -------------------------------------------------

    def undo(self) -> None:
        pass

    def redo(self) -> None:
        pass

    # -------------------------------------------------
    # Persistence
    # -------------------------------------------------

    def to_disk(
        self,
        nodes: set[Node],
    ) -> None:
        for node in nodes:
            if not self.metadata.is_persistent(
                node,
            ):
                continue

            self._storage.write(
                node.schema.name,
                node.value,
                store_version=self.config.store_version,
            )

    # -------------------------------------------------
    # Boot
    # -------------------------------------------------

    def boot(self) -> None:
        StoreBoot(
            self,
        ).boot()
