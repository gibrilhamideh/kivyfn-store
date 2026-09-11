# src/kivyfn/store/store/store.py
from __future__ import annotations

from collections import deque
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import replace
from threading import RLock
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from kivyfn.logging import get_logger
from kivyfn.store.address import Address
from kivyfn.store.core.engine import Engine
from kivyfn.store.core.execution import ExecutionResult
from kivyfn.store.error import (
    StoreErrorContext,
    StoreErrorStage,
    StoreOperationError,
    StoreStateError,
)
from kivyfn.store.events import StoreCommit
from kivyfn.store.journal import CommitJournal, JournalEntry
from kivyfn.store.protocol.message.requests import (
    InsertRequest,
    RemoveRequest,
    SetRequest,
    SetRowRequest,
)
from kivyfn.store.query import Query, QueryResult
from kivyfn.store.store.instructions import InstructionBuilder


if TYPE_CHECKING:
    from kivyfn.store.builder import StoreConfig
    from kivyfn.store.node import Node
    from kivyfn.store.query.plan import QueryPlan


logger = get_logger(__name__)


class Store:
    """Canonical synchronous state, execution, and commit publication.

    Each execute() call submits one atomic batch of typed mutation requests.
    The Store builds instructions, delegates execution to Engine, and records
    successful changes before notifying commit consumers. Host components use
    public Store methods for reads and synchronized() when several operations
    must share one revision.
    """

    def __init__(
        self, root: Node, config: StoreConfig, *, journal_size: int = 10_000
    ) -> None:
        self._engine = Engine(root=root, config=config)
        self._instructions = InstructionBuilder(self._engine.metadata)

        self._instance_id = uuid4().hex
        self._journal = CommitJournal(journal_size)

        self._lock = RLock()
        self._shutdown = False

        self._commit_listeners: dict[
            object, Callable[[StoreCommit], None]
        ] = {}
        self._pending_commits: deque[
            tuple[StoreCommit, tuple[object, ...]]
        ] = deque()
        self._publishing_commits = False

    # -------------------------------------------------
    # Lifecycle
    # -------------------------------------------------

    def boot(self) -> None:
        """Boot the Store once."""
        with self._lock:
            if self._shutdown:
                raise StoreStateError("A shut-down Store cannot be booted")

            self._engine.boot()

    def shutdown(self) -> None:
        """Permanently close this Store instance."""
        with self._lock:
            if self._shutdown:
                return

            self._shutdown = True
            self._commit_listeners.clear()
            self._pending_commits.clear()

    @property
    def booted(self) -> bool:
        with self._lock:
            return self._engine.booted and not self._shutdown

    @property
    def is_shutdown(self) -> bool:
        with self._lock:
            return self._shutdown

    @property
    def instance_id(self) -> str:
        return self._instance_id

    @property
    def revision(self) -> int:
        with self._lock:
            return self._journal.revision

    @property
    def journal_size(self) -> int:
        return self._journal.max_entries

    def _require_booted(self) -> None:
        if self._shutdown:
            raise StoreStateError("Store has been shut down")

        if not self._engine.booted:
            raise StoreStateError("Store has not been booted")

    @contextmanager
    def synchronized(self) -> Generator[None, None, None]:
        """Hold a consistent Store view across synchronous host operations.

        Read snapshots, capture live queries, and register subscriptions in
        this scope using the public Store methods. It exposes no Engine.

        The scope remains available after shutdown for Host cleanup; Store
        reads and writes enforce their own lifecycle. The lock is reentrant.
        Do not await or perform transport I/O here.
        """
        with self._lock:
            yield

    # -------------------------------------------------
    # Reads
    # -------------------------------------------------

    def get_node_by_address(self, address: Address) -> Node:
        """Resolve an internal address to its registered node."""
        with self.synchronized():
            self._require_booted()
            return self._engine.get_node_by_address(address)

    def get_node_by_schema(self, schema: str) -> Node:
        """Resolve a unique schema name to its registered node."""
        with self.synchronized():
            self._require_booted()
            return self._engine.get_node_by_schema(schema)

    def snapshot_by_address(
        self, address: Address, id: str | None = None, key: str | None = None
    ) -> Any:
        """Read a node, row, or field using its internal address."""
        with self.synchronized():
            self._require_booted()
            return self._engine.snapshot_by_address(address, id=id, key=key)

    def snapshot_by_schema(
        self, schema: str, id: str | None = None, key: str | None = None
    ) -> Any:
        """Read a node, row, or field using its unique schema name."""
        with self.synchronized():
            self._require_booted()
            return self._engine.snapshot_by_schema(schema, id=id, key=key)

    def query(self, query: Query[Any]) -> QueryResult:
        """Execute one Query against current canonical Store state."""
        with self.synchronized():
            self._require_booted()
            return self._engine.query(query)

    def compile_query(self, query: Query[Any]) -> QueryPlan:
        """Resolve a Query into a reusable plan for this Store."""
        with self.synchronized():
            self._require_booted()
            return self._engine.compile_query(query)

    def execute_query(self, plan: QueryPlan) -> QueryResult:
        """Evaluate a compiled plan against current canonical state."""
        with self.synchronized():
            self._require_booted()
            return self._engine.execute_query(plan)

    # -------------------------------------------------
    # Mutations
    # -------------------------------------------------

    def execute(
        self,
        requests: tuple[
            SetRequest | SetRowRequest | InsertRequest | RemoveRequest, ...
        ],
        *,
        operation_id: str | None = None,
    ) -> ExecutionResult:
        """Execute an ordered mutation batch as one atomic transaction.

        Request decoding belongs to the codec. InstructionBuilder resolves
        schemas; Engine compiles instructions and validates the complete batch
        before applying it. Rejected batches never reach the journal or feed.
        """
        with self.synchronized():
            self._require_booted()

            try:
                instructions = self._instructions.build(requests)
            except StoreOperationError as error:
                error.context = replace(
                    error.context or StoreErrorContext(),
                    stage=StoreErrorStage.RESOLVE,
                )
                return ExecutionResult(
                    success=False, stage=StoreErrorStage.RESOLVE, error=error
                )

            result = self._engine.execute(
                instructions, operation_id=operation_id
            )

            if not result.success:
                return result

            commit = self._record_commit(cast(StoreCommit, result.commit))
            return replace(result, commit=commit)

    # -------------------------------------------------
    # Replay
    # -------------------------------------------------

    def replay_after(self, revision: int) -> tuple[StoreCommit, ...] | None:
        """
        Return retained commits after revision.

        None means the requested gap is no longer replayable and a fresh
        snapshot is required.
        """
        with self.synchronized():
            self._require_booted()
            entries = self._journal.replay_after(revision)

            if entries is None:
                return None

            return tuple(self._stamp_entry(entry) for entry in entries)

    # -------------------------------------------------
    # Undo / redo
    # -------------------------------------------------

    def undo(self, *, operation_id: str | None = None) -> StoreCommit:
        with self.synchronized():
            self._require_booted()
            commit = self._engine.undo(operation_id=operation_id)

            if commit is None:
                raise StoreStateError("There is no Store commit to undo")

            return self._record_commit(commit)

    def redo(self, *, operation_id: str | None = None) -> StoreCommit:
        with self.synchronized():
            self._require_booted()
            commit = self._engine.redo(operation_id=operation_id)

            if commit is None:
                raise StoreStateError("There is no Store commit to redo")

            return self._record_commit(commit)

    @property
    def can_undo(self) -> bool:
        with self.synchronized():
            self._require_booted()
            return self._engine.can_undo

    @property
    def can_redo(self) -> bool:
        with self.synchronized():
            self._require_booted()
            return self._engine.can_redo

    # -------------------------------------------------
    # Commit feed
    # -------------------------------------------------

    def subscribe_commits(
        self, callback: Callable[[StoreCommit], None]
    ) -> Callable[[], None]:
        """
        Attach a synchronous consumer of future commits.

        Callbacks run synchronously on the committing thread while the
        Store lock is held. Capture subscription data and enqueue delivery
        without waiting for transport I/O. Notifications follow revision
        order; an earlier callback can still write reentrantly, so a live
        query consumer must capture only at the current Store revision.

        The returned function detaches the consumer and is idempotent.
        """
        with self.synchronized():
            self._require_booted()
            token = object()
            self._commit_listeners[token] = callback

        def unsubscribe() -> None:
            with self._lock:
                self._commit_listeners.pop(token, None)

        return unsubscribe

    def _publish_commit(self, commit: StoreCommit) -> None:
        """Queue a stamped commit and drain callbacks under the Store lock."""
        self._pending_commits.append((commit, tuple(self._commit_listeners)))

        if self._publishing_commits:
            return

        self._publishing_commits = True

        try:
            while self._pending_commits:
                current, tokens = self._pending_commits.popleft()

                for token in tokens:
                    callback = self._commit_listeners.get(token)

                    if callback is None:
                        continue

                    try:
                        callback(current)
                    except Exception:
                        logger.exception(
                            "Store commit callback failed at revision %s",
                            current.revision,
                        )
        finally:
            self._publishing_commits = False

    # -------------------------------------------------
    # Revision stamping
    # -------------------------------------------------

    def _record_commit(self, commit: StoreCommit) -> StoreCommit:
        if not commit.events:
            return replace(
                commit,
                server_instance_id=self._instance_id,
                revision=self._journal.revision,
            )

        entry = self._journal.record(commit)
        stamped = self._stamp_entry(entry)
        self._publish_commit(stamped)

        return stamped

    def _stamp_entry(self, entry: JournalEntry) -> StoreCommit:
        return replace(
            entry.commit,
            server_instance_id=self._instance_id,
            revision=entry.revision,
        )
