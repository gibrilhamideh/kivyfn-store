# src/kivyfn/store/store/store.py
from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import replace
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
from kivyfn.store.query import Query, QueryResult
from kivyfn.store.store.instructions import (
    InstructionBuilder,
    StoreRequestAction,
)


if TYPE_CHECKING:
    from kivyfn.store.builder import StoreConfig
    from kivyfn.store.node import Node
    from kivyfn.store.query.plan import QueryPlan


logger = get_logger(__name__)


class Store:
    """Canonical synchronous Store state and execution."""

    def __init__(self, root: Node, config: StoreConfig) -> None:
        self._engine = Engine(root=root, config=config)
        self._instructions = InstructionBuilder(self._engine.metadata)

        self._instance_id = uuid4().hex
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
        if self._shutdown:
            raise StoreStateError("A shut-down Store cannot be booted")

        logger.info("Booting Store instance=%s", self._instance_id)

        self._engine.boot()

        logger.info(
            "Store booted instance=%s schemas=%d",
            self._instance_id,
            len(self._engine.metadata.nodes_by_schema),
        )

    def shutdown(self) -> None:
        if self._shutdown:
            return

        logger.info("Shutting down Store instance=%s", self._instance_id)

        self._shutdown = True
        self._commit_listeners.clear()
        self._pending_commits.clear()

    @property
    def booted(self) -> bool:
        return self._engine.booted and not self._shutdown

    @property
    def is_shutdown(self) -> bool:
        return self._shutdown

    @property
    def instance_id(self) -> str:
        return self._instance_id

    @property
    def schema_revisions(self) -> dict[str, int]:
        self._require_booted()

        return {
            schema: node.revision
            for schema, node in self._engine.metadata.nodes_by_schema.items()
        }

    def _require_booted(self) -> None:
        if self._shutdown:
            raise StoreStateError("Store has been shut down")

        if not self._engine.booted:
            raise StoreStateError("Store has not been booted")

    # -------------------------------------------------
    # Reads
    # -------------------------------------------------

    def get_node_by_address(self, address: Address) -> Node:
        self._require_booted()
        return self._engine.get_node_by_address(address)

    def get_node_by_schema(self, schema: str) -> Node:
        self._require_booted()
        return self._engine.get_node_by_schema(schema)

    def snapshot_by_address(
        self, address: Address, id: str | None = None, key: str | None = None
    ) -> Any:
        self._require_booted()
        return self._engine.snapshot_by_address(address, id=id, key=key)

    def snapshot_by_schema(
        self, schema: str, id: str | None = None, key: str | None = None
    ) -> Any:
        self._require_booted()
        return self._engine.snapshot_by_schema(schema, id=id, key=key)

    def snapshot_if_changed(
        self,
        schema: str,
        *,
        revision: int | None = None,
        store_instance_id: str | None = None,
    ) -> tuple[int, dict[str, Any] | None]:
        self._require_booted()

        node = self._engine.get_node_by_schema(schema)
        current = node.revision

        if store_instance_id == self._instance_id and revision == current:
            return current, None

        return current, node.snapshot()

    def query(self, query: Query[Any]) -> QueryResult:
        self._require_booted()
        return self._engine.query(query)

    def compile_query(self, query: Query[Any]) -> QueryPlan:
        self._require_booted()
        return self._engine.compile_query(query)

    def execute_query(self, plan: QueryPlan) -> QueryResult:
        self._require_booted()
        return self._engine.execute_query(plan)

    # -------------------------------------------------
    # Transactions
    # -------------------------------------------------

    def execute(
        self, actions: tuple[StoreRequestAction, ...], *, transaction_id: str
    ) -> ExecutionResult:
        """Execute Store mutation actions as one atomic transaction."""
        self._require_booted()

        logger.info(
            "Transaction start id=%s actions=%d", transaction_id, len(actions)
        )

        try:
            instructions = self._instructions.build(actions)

        except StoreOperationError as error:
            error.context = replace(
                error.context or StoreErrorContext(),
                stage=StoreErrorStage.RESOLVE,
            )

            logger.warning(
                "Transaction resolve failed id=%s error=%r",
                transaction_id,
                error,
            )

            return ExecutionResult(
                success=False, stage=StoreErrorStage.RESOLVE, error=error
            )

        logger.debug(
            "Transaction resolved id=%s instructions=%d",
            transaction_id,
            len(instructions),
        )

        try:
            result = self._engine.execute(
                instructions, transaction_id=transaction_id
            )

            if not result.success:
                logger.warning(
                    "Transaction failed id=%s stage=%s error=%r",
                    transaction_id,
                    result.stage,
                    result.error,
                )
                return result

            commit = self._record_commit(cast(StoreCommit, result.commit))

            logger.info(
                "Transaction committed id=%s events=%d revisions=%r",
                transaction_id,
                len(commit.events),
                commit.schema_revisions,
            )

            return replace(result, commit=commit)

        except Exception:
            logger.exception(
                "Transaction crashed id=%s; shutting down Store",
                transaction_id,
            )

            self.shutdown()
            raise

    # -------------------------------------------------
    # Commit feed
    # -------------------------------------------------

    def subscribe_commits(
        self, callback: Callable[[StoreCommit], None]
    ) -> Callable[[], None]:
        """Subscribe to future Store commits."""
        self._require_booted()

        token = object()
        self._commit_listeners[token] = callback

        logger.debug(
            "Commit listener added listeners=%d", len(self._commit_listeners)
        )

        def unsubscribe() -> None:
            self._commit_listeners.pop(token, None)

            logger.debug(
                "Commit listener removed listeners=%d",
                len(self._commit_listeners),
            )

        return unsubscribe

    def _publish_commit(self, commit: StoreCommit) -> None:
        self._pending_commits.append((commit, tuple(self._commit_listeners)))

        if self._publishing_commits:
            return

        self._publishing_commits = True

        try:
            while self._pending_commits:
                current, tokens = self._pending_commits.popleft()

                logger.debug(
                    "Publishing commit transaction=%s listeners=%d events=%d",
                    current.transaction_id,
                    len(tokens),
                    len(current.events),
                )

                for token in tokens:
                    callback = self._commit_listeners.get(token)

                    if callback is None:
                        continue

                    try:
                        callback(current)

                    except Exception:
                        logger.exception(
                            "Store commit callback failed transaction=%s "
                            "schemas=%r",
                            current.transaction_id,
                            current.schema_revisions,
                        )

        finally:
            self._publishing_commits = False

    # -------------------------------------------------
    # Revisions
    # -------------------------------------------------

    def _record_commit(self, commit: StoreCommit) -> StoreCommit:
        """Advance each changed schema once for this commit."""
        schemas = dict.fromkeys(event.schema for event in commit.events)

        nodes = {
            schema: self._engine.get_node_by_schema(schema)
            for schema in schemas
        }

        revisions = {
            schema: node.advance_revision() for schema, node in nodes.items()
        }

        stamped = replace(
            commit,
            server_instance_id=self._instance_id,
            schema_revisions=revisions,
        )

        if stamped.events:
            self._publish_commit(stamped)

        return stamped
