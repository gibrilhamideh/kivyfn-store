# kivyfn/store/core/engine.py
from __future__ import annotations

from threading import RLock
from typing import TYPE_CHECKING, Any, TypeVar

from kivyfn.store.address import Address
from kivyfn.store.core.boot import StoreBoot
from kivyfn.store.core.execution import Execution, ExecutionResult
from kivyfn.store.core.metadata import EngineMetadata
from kivyfn.store.storage import FileStorage


if TYPE_CHECKING:
    from kivyfn.store.builder import StoreConfig
    from kivyfn.store.core.operations import Operation
    from kivyfn.store.events.event import StoreEvent
    from kivyfn.store.node import Node


T = TypeVar("T")


class Engine:
    """
    Synchronous store execution engine.

    The engine owns:
    - node metadata
    - synchronous reads
    - transaction execution
    - persistence
    - undo and redo history

    The engine does not own:
    - transports
    - client connections
    - subscriptions
    - signal bindings
    - process lifecycle
    - asyncio tasks

    All public operations are protected by a reentrant lock so an Engine
    instance can safely be used by LocalStore from multiple threads.
    """

    def __init__(
        self,
        root: Node,
        config: StoreConfig,
    ) -> None:
        self.config = config
        self.metadata = EngineMetadata.build(root)

        self._storage = FileStorage(
            self.config.storage_path,
        )

        self._history: list[list[Operation]] = []
        self._redo: list[list[Operation]] = []

        self._lock = RLock()
        self._booted = False

    # -------------------------------------------------
    # Lifecycle
    # -------------------------------------------------

    def boot(self) -> None:
        """
        Load and validate persistent state.

        An Engine instance may only be booted once.
        """
        with self._lock:
            if self._booted:
                raise RuntimeError("Store engine is already booted")

            StoreBoot(
                self,
            ).boot()

            self._booted = True

    @property
    def booted(self) -> bool:
        with self._lock:
            return self._booted

    def _require_booted(self) -> None:
        if not self._booted:
            raise RuntimeError(
                "Store engine must be booted before use"
            )

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
    # Reads
    # -------------------------------------------------

    def snapshot(
        self,
        address: Address[T],
        id: str | None = None,
        key: str | None = None,
        *,
        resolve_foreignkeys: bool = False,
    ) -> Any:
        with self._lock:
            self._require_booted()

            node = self.get_node_by_address(
                address,
            )

            data = node.snapshot(
                id=id,
                key=key,
            )

            if not resolve_foreignkeys:
                return data

            if data is None or key is not None:
                return data

            if node.schema.many and id is None:
                return {
                    row_id: self._resolve_foreignkeys(
                        node=node,
                        row=row,
                        visited={
                            (
                                node.schema.name,
                                row_id,
                            )
                        },
                    )
                    for row_id, row in data.items()
                }

            row_id = id if node.schema.many else None

            return self._resolve_foreignkeys(
                node=node,
                row=data,
                visited={
                    (
                        node.schema.name,
                        row_id,
                    )
                },
            )

    def lookup(
        self,
        address: Address[T],
        fields: str | tuple[str, ...],
        value: Any,
        *,
        first: bool = False,
    ) -> Any:
        with self._lock:
            self._require_booted()

            node = self.get_node_by_address(
                address,
            )

            lookup_fields = (
                (fields,)
                if isinstance(fields, str)
                else fields
            )

            if first:
                row_id = node.lookup(
                    fields=lookup_fields,
                    value=value,
                    first=True,
                )

                if row_id is None:
                    return None

                return node.snapshot(
                    id=row_id,
                )

            row_ids = node.lookup(
                fields=lookup_fields,
                value=value,
                first=False,
            )

            return {
                row_id: node.snapshot(
                    id=row_id,
                )
                for row_id in row_ids
            }

    def to_dict(
        self,
        address: Address[T],
    ) -> Any:
        with self._lock:
            self._require_booted()

            node = self.get_node_by_address(
                address,
            )

            return node.snapshot()

    # -------------------------------------------------
    # Foreign-key resolution
    # -------------------------------------------------

    def resolve_foreignkeys(
        self,
        node: Node,
        row: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Resolve foreign-key rows into a flattened dictionary.

        Cyclic foreign-key graphs are detected and do not recurse forever.
        """
        with self._lock:
            self._require_booted()

            return self._resolve_foreignkeys(
                node=node,
                row=row,
                visited=set(),
            )

    def _resolve_foreignkeys(
        self,
        *,
        node: Node,
        row: dict[str, Any],
        visited: set[tuple[str, str | None]],
    ) -> dict[str, Any]:
        resolved = dict(row)

        for foreignkey, field in node.schema.foreign_key_fields().items():
            foreign_id = row.get(
                foreignkey,
            )

            if foreign_id is None:
                continue

            referenced_node = self.get_node_by_schema(
                field.schema,
            )

            reference = (
                referenced_node.schema.name,
                str(foreign_id),
            )

            if reference in visited:
                continue

            referenced_row = referenced_node.snapshot(
                id=str(foreign_id),
            )

            if not isinstance(referenced_row, dict):
                continue

            nested_visited = set(
                visited,
            )
            nested_visited.add(
                reference,
            )

            resolved.update(
                self._resolve_foreignkeys(
                    node=referenced_node,
                    row=referenced_row,
                    visited=nested_visited,
                )
            )

        return resolved

    # -------------------------------------------------
    # Transactions
    # -------------------------------------------------

    def execute(
        self,
        commands: list[dict[str, Any]],
    ) -> ExecutionResult:
        """
        Compile, validate, and execute a transaction atomically with respect
        to other Engine callers.

        Failed compilation or validation does not mutate or persist state.
        """
        with self._lock:
            self._require_booted()

            result = Execution(
                self,
                commands,
            ).execute()

            if not result.success:
                return result

            self.to_disk(
                result.touched_nodes,
            )

            operations = list(
                result.compiled,
            )

            if operations:
                self._history.append(
                    operations,
                )
                self._redo.clear()

            return result

    # -------------------------------------------------
    # Undo / Redo
    # -------------------------------------------------

    def undo(self) -> list[StoreEvent]:
        """
        Undo the most recent successful transaction.

        Returns the events produced by the inverse operations.
        """
        with self._lock:
            self._require_booted()

            if not self._history:
                return []

            operations = self._history.pop()
            events: list[StoreEvent] = []

            for operation in reversed(
                operations,
            ):
                event = operation.undo()

                if event is not None:
                    events.append(
                        event,
                    )

            self.to_disk(
                {
                    operation.node
                    for operation in operations
                }
            )

            self._redo.append(
                operations,
            )

            return events

    def redo(self) -> list[StoreEvent]:
        """
        Reapply the most recently undone transaction.

        Returns the events produced by the reapplied operations.
        """
        with self._lock:
            self._require_booted()

            if not self._redo:
                return []

            operations = self._redo.pop()
            events: list[StoreEvent] = []

            for operation in operations:
                event = operation.execute()

                if event is not None:
                    events.append(
                        event,
                    )

            self.to_disk(
                {
                    operation.node
                    for operation in operations
                }
            )

            self._history.append(
                operations,
            )

            return events

    @property
    def can_undo(self) -> bool:
        with self._lock:
            return bool(
                self._history,
            )

    @property
    def can_redo(self) -> bool:
        with self._lock:
            return bool(
                self._redo,
            )

    # -------------------------------------------------
    # Persistence
    # -------------------------------------------------

    def to_disk(
        self,
        nodes: set[Node],
    ) -> None:
        """
        Persist the supplied nodes.

        Callers must already hold the engine lock when this participates in a
        larger operation. RLock also permits safe direct use.
        """
        with self._lock:
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
