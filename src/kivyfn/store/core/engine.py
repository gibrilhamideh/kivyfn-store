# src/kivyfn/store/core/engine.py
from __future__ import annotations

from collections.abc import Iterable
from threading import RLock
from typing import TYPE_CHECKING, Any

from kivyfn.store.address import Address
from kivyfn.store.core.boot import StoreBoot
from kivyfn.store.core.execution import Execution, ExecutionResult
from kivyfn.store.core.instruction import Instruction
from kivyfn.store.core.metadata import EngineMetadata
from kivyfn.store.error import StoreStateError
from kivyfn.store.query.plan import QueryPlan
from kivyfn.store.storage import FileStorage


if TYPE_CHECKING:
    from kivyfn.store.builder import StoreConfig
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
    - persistence.

    The engine does not own transports, subscriptions, reactive State,
    signals, process lifecycle, or asyncio tasks.
    """

    def __init__(self, root: Node, config: StoreConfig) -> None:
        self.config = config
        self.metadata = EngineMetadata.build(root)

        self._storage = FileStorage(config.storage_path)

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

            self.to_disk(result.touched_nodes)

            return result

    # -------------------------------------------------
    # Persistence
    # -------------------------------------------------

    def to_disk(self, nodes: Iterable[Node]) -> None:
        """Write each supplied persistent node once, immediately."""
        with self._lock:
            for node in dict.fromkeys(nodes):
                if not self.metadata.is_persistent(node):
                    continue

                self._write_node(node, node.value)

    def _write_node(self, node: Node, data: dict[str, Any]) -> None:
        self._storage.write(
            node.schema.name, data, store_version=self.config.store_version
        )
