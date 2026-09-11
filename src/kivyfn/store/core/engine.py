# src/kivyfn/store/core/engine.py
from __future__ import annotations

from collections.abc import Callable, Iterable
from threading import RLock
from typing import TYPE_CHECKING, Any

from kivyfn.store.address import Address
from kivyfn.store.core.boot import StoreBoot
from kivyfn.store.core.execution import Execution, ExecutionResult
from kivyfn.store.core.instruction import Instruction
from kivyfn.store.core.metadata import EngineMetadata
from kivyfn.store.error import StoreStateError
from kivyfn.store.events.event import StoreCommit
from kivyfn.store.query.plan import QueryPlan
from kivyfn.store.storage import (
    FileStorage,
    PersistenceSnapshot,
    PersistenceTracker,
)


if TYPE_CHECKING:
    from kivyfn.store.builder import StoreConfig
    from kivyfn.store.core.operations import Operation
    from kivyfn.store.node import Node
    from kivyfn.store.query import Query, QueryResult


class Engine:
    """
    Synchronous Store execution engine.

    The engine owns:
    - Store node metadata;
    - synchronous snapshots by schema name or address;
    - one-shot Query execution;
    - atomic transaction execution;
    - persistence;
    - undo and redo history.

    The engine does not own transports, subscriptions, reactive State,
    signals, process lifecycle, or asyncio tasks.
    """

    def __init__(self, root: Node, config: StoreConfig) -> None:
        self.config = config
        self.metadata = EngineMetadata.build(root)

        self._storage = FileStorage(config.storage_path)
        self._persistence = PersistenceTracker()

        self._history: list[tuple[Operation, ...]] = []
        self._redo: list[tuple[Operation, ...]] = []

        self._lock = RLock()
        self._booted = False

    # -------------------------------------------------
    # Lifecycle
    # -------------------------------------------------

    def boot(self) -> None:
        with self._lock:
            if self._booted:
                raise StoreStateError("Store engine is already booted")

            StoreBoot(self).boot()

            self._booted = True

    @property
    def booted(self) -> bool:
        with self._lock:
            return self._booted

    def _require_booted(self) -> None:
        if not self._booted:
            raise StoreStateError("Store engine must be booted before use")

    # -------------------------------------------------
    # Node lookup
    # -------------------------------------------------

    def get_node_by_address(self, address: Address) -> Node:
        return self.metadata.get_node_by_address(address)

    def get_node_by_schema(self, name: str) -> Node:
        return self.metadata.get_node_by_schema(name)

    # -------------------------------------------------
    # Reads
    # -------------------------------------------------

    def snapshot_by_address(
        self, address: Address, id: str | None = None, key: str | None = None
    ) -> Any:
        """Read a node, row, or field using its internal address."""
        with self._lock:
            self._require_booted()

            node = self.get_node_by_address(address)

            return node.snapshot(id=id, key=key)

    def snapshot_by_schema(
        self, schema: str, id: str | None = None, key: str | None = None
    ) -> Any:
        """Read a node, row, or field using its unique schema name."""
        with self._lock:
            self._require_booted()

            node = self.get_node_by_schema(schema)

            return node.snapshot(id=id, key=key)

    # -------------------------------------------------
    # Queries
    # -------------------------------------------------

    def compile_query(self, query: Query[Any]) -> QueryPlan:
        with self._lock:
            self._require_booted()

            return QueryPlan.compile(self, query)

    def execute_query(self, plan: QueryPlan) -> QueryResult:
        with self._lock:
            self._require_booted()

            return plan.execute()

    def query(self, query: Query[Any]) -> QueryResult:
        with self._lock:
            self._require_booted()

            plan = QueryPlan.compile(self, query)

            return plan.execute()

    # -------------------------------------------------
    # Transactions
    # -------------------------------------------------

    def execute(
        self,
        instructions: tuple[Instruction, ...],
        *,
        operation_id: str | None = None,
    ) -> ExecutionResult:
        """
        Compile, validate, and execute one atomic batch of instructions.

        Compilation or validation failure does not mutate canonical Store
        data.
        """
        with self._lock:
            self._require_booted()

            execution = Execution(
                self, instructions, operation_id=operation_id
            )
            result = execution.execute()

            if not result.success:
                return result

            self.schedule_persistence(result.touched_nodes)

            if result.compiled:
                self._history.append(result.compiled)
                self._redo.clear()

            return result

    # -------------------------------------------------
    # Undo / Redo
    # -------------------------------------------------

    def undo(self, *, operation_id: str | None = None) -> StoreCommit | None:
        with self._lock:
            self._require_booted()

            if not self._history:
                return None

            operations = self._history.pop()

            events = tuple(
                operation.undo() for operation in reversed(operations)
            )

            self.schedule_persistence(
                operation.node for operation in operations
            )

            self._redo.append(operations)

            return StoreCommit(events=events, operation_id=operation_id)

    def redo(self, *, operation_id: str | None = None) -> StoreCommit | None:
        with self._lock:
            self._require_booted()

            if not self._redo:
                return None

            operations = self._redo.pop()

            events = tuple(operation.execute() for operation in operations)

            self.schedule_persistence(
                operation.node for operation in operations
            )

            self._history.append(operations)

            return StoreCommit(events=events, operation_id=operation_id)

    @property
    def can_undo(self) -> bool:
        with self._lock:
            return bool(self._history)

    @property
    def can_redo(self) -> bool:
        with self._lock:
            return bool(self._redo)

    # -------------------------------------------------
    # Persistence
    # -------------------------------------------------

    def schedule_persistence(self, nodes: Iterable[Node]) -> None:
        """Persist immediate nodes and schedule delayed nodes."""
        with self._lock:
            for node in nodes:
                if not self.metadata.is_persistent(node):
                    continue

                interval = self.metadata.persistence_interval(node)

                if interval is None:
                    self._write_node(node, node.value)
                    continue

                self._persistence.mark_dirty(node, interval)

    def to_disk(self, nodes: Iterable[Node]) -> None:
        """Force supplied persistent nodes to disk immediately."""
        with self._lock:
            for node in nodes:
                if not self.metadata.is_persistent(node):
                    continue

                self._write_node(node, node.value)

    def set_persistence_waker(
        self, callback: Callable[[], None] | None
    ) -> None:
        with self._lock:
            self._persistence.set_waker(callback)

    def capture_due_persistence(
        self, *, force: bool = False
    ) -> tuple[PersistenceSnapshot, ...]:
        with self._lock:
            self._require_booted()

            return self._persistence.capture_due(force=force)

    def write_persistence_snapshot(
        self, snapshot: PersistenceSnapshot
    ) -> None:
        with self._lock:
            self._write_node(snapshot.node, snapshot.data)

    def complete_persistence_snapshot(
        self, snapshot: PersistenceSnapshot, *, success: bool
    ) -> None:
        with self._lock:
            self._persistence.complete(snapshot, success=success)

    def next_persistence_deadline(self) -> float | None:
        with self._lock:
            return self._persistence.next_deadline()

    @property
    def has_delayed_persistence(self) -> bool:
        with self._lock:
            return bool(self.metadata.persistence_intervals)

    @property
    def has_pending_persistence(self) -> bool:
        with self._lock:
            return self._persistence.has_pending

    @property
    def pending_persistence_count(self) -> int:
        with self._lock:
            return self._persistence.pending_count

    def _write_node(self, node: Node, data: dict[str, Any]) -> None:
        self._storage.write(
            node.schema.name, data, store_version=self.config.store_version
        )
