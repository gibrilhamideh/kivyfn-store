# src/kivyfn/store/store/store.py
from __future__ import annotations

from asyncio import AbstractEventLoop, get_running_loop
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from threading import RLock
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

from kivyfn.logging import get_logger

from kivyfn.store.address import Address
from kivyfn.store.core.delta import Delta
from kivyfn.store.core.engine import Engine
from kivyfn.store.core.execution import ExecutionResult
from kivyfn.store.core.operations import Operation
from kivyfn.store.error import (
    HistoryError,
    HistoryErrorDetail,
    StoreErrorCode,
    StoreErrorContext,
    StoreErrorStage,
    StoreOperationError,
    StoreStateError,
)
from kivyfn.store.events import StoreCommit
from kivyfn.store.events.event import SignalRowEvent
from kivyfn.store.query import Query, QueryResult
from kivyfn.store.schema import ManySchema
from kivyfn.store.store.history import (
    History,
    HistoryPage,
    HistoryPosition,
    HistoryStatus,
)
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
    """Canonical synchronous Store state and execution.

    Public reads, mutations, revisions and lifecycle transitions share one
    reentrant lock. Each read observes a complete recorded commit. Separate
    calls do not form a read transaction; use snapshot_if_changed() when the
    schema snapshot and its revision must be returned together.
    """

    def __init__(self, config: StoreConfig) -> None:
        root = config.root._build_node()
        self._engine = Engine(root=root, config=config)
        self._instructions = InstructionBuilder(self._engine.metadata)

        self._instance_id = uuid4().hex
        self._shutdown = False
        self._lock = RLock()

        self._commit_listeners: dict[
            object, Callable[[StoreCommit], None]
        ] = {}

        self._history = History(self._instance_id, config.history)
        self._history_listeners: dict[
            object, Callable[[HistoryStatus], None]
        ] = {}
        self._publishing_commits = False
        self._host_owner: object | None = None
        self._host_loop: AbstractEventLoop | None = None

    # -------------------------------------------------
    # Lifecycle
    # -------------------------------------------------

    @property
    def config(self) -> StoreConfig:
        """Configuration used to construct this Store."""
        return self._engine.config

    def boot(self) -> None:
        with self._lock:
            self._require_mutation_owner()
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
        with self._lock:
            self._require_host_loop()
            if self._shutdown:
                return

            logger.info("Shutting down Store instance=%s", self._instance_id)

            self._shutdown = True
            self._commit_listeners.clear()
            self._history_listeners.clear()

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
    def schema_revisions(self) -> dict[str, int]:
        with self._lock:
            self._require_booted()
            return {
                schema: node.revision
                for schema, node in (
                    self._engine.metadata.nodes_by_schema.items()
                )
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
        """Borrow an internal Node; application mutations use execute()."""
        with self._lock:
            self._require_booted()
            return self._engine.get_node_by_address(address)

    def get_node_by_schema(self, schema: str) -> Node:
        """Borrow an internal Node; application mutations use execute()."""
        with self._lock:
            self._require_booted()
            return self._engine.get_node_by_schema(schema)

    def snapshot_by_address(
        self, address: Address, id: str | None = None, key: str | None = None
    ) -> Any:
        with self._lock:
            self._require_booted()
            return self._engine.snapshot_by_address(address, id=id, key=key)

    def snapshot_by_schema(
        self, schema: str, id: str | None = None, key: str | None = None
    ) -> Any:
        with self._lock:
            self._require_booted()
            return self._engine.snapshot_by_schema(schema, id=id, key=key)

    def snapshot_if_changed(
        self,
        schema: str,
        *,
        revision: int | None = None,
        store_instance_id: str | None = None,
    ) -> tuple[int, dict[str, Any] | None]:
        with self._lock:
            self._require_booted()
            node = self._engine.get_node_by_schema(schema)
            current = node.revision

            if store_instance_id == self._instance_id and revision == current:
                return current, None

            return current, node.snapshot()

    def query(self, query: Query[Any]) -> QueryResult:
        with self._lock:
            self._require_booted()
            return self._engine.query(query)

    def compile_query(self, query: Query[Any]) -> QueryPlan:
        with self._lock:
            self._require_booted()
            return self._engine.compile_query(query)

    def execute_query(self, plan: QueryPlan) -> QueryResult:
        with self._lock:
            self._require_booted()
            return self._engine.execute_query(plan)

    # -------------------------------------------------
    # Transactions
    # -------------------------------------------------

    def execute(
        self,
        actions: tuple[StoreRequestAction, ...],
        *,
        transaction_id: str,
        origin_client_id: str | None = None,
        history_group: str | None = None,
        history_label: str | None = None,
        expected_store_instance_id: str | None = None,
        expected_revisions: dict[str, int] | None = None,
    ) -> ExecutionResult:
        """Execute on the owning loop, with read-only synchronous observers."""
        with self._lock:
            self._require_mutation_owner()
            self._require_booted()
            try:
                if expected_store_instance_id is not None and (
                    expected_store_instance_id != self.instance_id
                ):
                    raise HistoryError(
                        "Store instance changed",
                        code=StoreErrorCode.HISTORY_STALE,
                    )
                for schema, revision in (expected_revisions or {}).items():
                    if (
                        self._engine.get_node_by_schema(schema).revision
                        != revision
                    ):
                        raise HistoryError(
                            "Displayed schema revision is stale",
                            code=StoreErrorCode.HISTORY_STALE,
                            detail=HistoryErrorDetail(schema=schema),
                        )
                instructions, signals = self._instructions.build(actions)
            except StoreOperationError as error:
                return self._failure(error, StoreErrorStage.RESOLVE)

            prepared_history = self._history.state
            entry_id: str | None = None

            def prepare(delta: Delta, result: ExecutionResult) -> None:
                nonlocal prepared_history, entry_id
                prepared_history, entry_id = self._history.prepare(
                    self._engine,
                    delta,
                    result,
                    signals,
                    transaction_id=transaction_id,
                    origin_client_id=origin_client_id,
                    group=history_group,
                    label=history_label,
                )

            try:
                result = self._engine.execute(
                    instructions,
                    transaction_id=transaction_id,
                    validate=lambda delta: self._instructions.validate_signals(
                        signals, delta
                    ),
                    prepare=prepare,
                )
                if not result.success:
                    return result
                changed = (
                    prepared_history.revision != self._history.state.revision
                )
                self._history.state = prepared_history
                commit = replace(
                    cast(StoreCommit, result.commit),
                    signals=signals,
                    origin_client_id=origin_client_id,
                    history_entry_id=entry_id,
                    history_group=history_group,
                    history_label=history_label,
                )
                return replace(
                    result, commit=self._publish_transaction(commit, changed)
                )
            except Exception:
                logger.exception("Transaction crashed id=%s", transaction_id)
                self.shutdown()
                raise

    @staticmethod
    def _failure(
        error: StoreOperationError, stage: StoreErrorStage
    ) -> ExecutionResult:
        error.context = replace(
            error.context or StoreErrorContext(), stage=stage
        )
        return ExecutionResult(success=False, stage=stage, error=error)

    def claim_host(self, owner: object, loop: AbstractEventLoop) -> None:
        """Bind all hosted mutations to the Host's actual running loop."""
        with self._lock:
            self._require_mutation_owner()
            if self._host_owner is not None and self._host_owner is not owner:
                raise StoreStateError(
                    "Store already belongs to an active Host"
                )
            if get_running_loop() is not loop:
                raise StoreStateError(
                    "Host must claim Store on its event loop"
                )
            self._host_owner, self._host_loop = owner, loop

    def release_host(self, owner: object) -> None:
        with self._lock:
            if self._host_owner is not owner:
                return
            self._require_mutation_owner()
            self._host_owner = self._host_loop = None

    def _require_mutation_owner(self) -> None:
        if self._publishing_commits:
            raise StoreStateError(
                "Store mutations are not allowed during commit delivery "
                "or history publication"
            )
        self._require_host_loop()

    def _require_host_loop(self) -> None:
        if self._host_loop is None:
            return
        try:
            loop = get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not self._host_loop:
            raise StoreStateError(
                "Hosted Store mutations must execute on the Host event loop; "
                "schedule the complete operation on that loop"
            )

    def history(self, limit: int = 100, offset: int = 0) -> HistoryPage:
        with self._lock:
            self._require_booted()
            if type(limit) is not int or not 1 <= limit <= 1000:
                raise ValueError("History limit must be between 1 and 1000")
            if type(offset) is not int or offset < 0:
                raise ValueError("History offset must be nonnegative")
            return self._history.page(limit, offset)

    def close_history_group(self, origin_client_id: str | None = None) -> None:
        with self._lock:
            self._require_mutation_owner()
            revision = self._history.state.revision
            self._history.close_group(origin_client_id)
            if self._history.state.revision != revision:
                self._publishing_commits = True
                try:
                    self._publish_history()
                finally:
                    self._publishing_commits = False

    def undo(
        self,
        *,
        transaction_id: str,
        expected_position: HistoryPosition,
        origin_client_id: str | None = None,
    ) -> ExecutionResult:
        return self._navigate(
            "undo", transaction_id, expected_position, origin_client_id
        )

    def redo(
        self,
        *,
        transaction_id: str,
        expected_position: HistoryPosition,
        origin_client_id: str | None = None,
    ) -> ExecutionResult:
        return self._navigate(
            "redo", transaction_id, expected_position, origin_client_id
        )

    def _navigate(
        self,
        kind: Literal["undo", "redo"],
        transaction_id: str,
        expected_position: HistoryPosition,
        origin_client_id: str | None,
    ) -> ExecutionResult:
        with self._lock:
            self._require_mutation_owner()
            self._require_booted()
            status = self._history.status()
            undo = kind == "undo"
            position = status.undo_position if undo else status.redo_position
            try:
                if not status.enabled:
                    raise HistoryError(
                        "History is disabled",
                        code=StoreErrorCode.HISTORY_DISABLED,
                    )
                if expected_position != position:
                    raise HistoryError(
                        "History position is stale",
                        code=StoreErrorCode.HISTORY_STALE,
                    )
                if position.entry_id is None:
                    raise HistoryError(
                        status.barrier or "No history entry is available",
                        code=(
                            StoreErrorCode.HISTORY_BLOCKED
                            if status.barrier
                            else StoreErrorCode.HISTORY_EMPTY
                        ),
                    )
                state = self._history.state
                index = state.cursor - 1 if undo else state.cursor
                entry = state.entries[index]
                for record in entry.records:
                    record.guard(self._engine, undo=undo)
                # A separately deleted signal target conflicts even if the
                # recorded restoration would happen to recreate that ID.
                for occurrence in entry.signals:
                    signal = occurrence.forward
                    if isinstance(
                        signal, SignalRowEvent
                    ) and signal.id not in (
                        self._engine.get_node_by_schema(signal.schema).value
                    ):
                        raise HistoryError(
                            "Recorded signal target was removed"
                        )
                delta = Delta()
                records = reversed(entry.records) if undo else entry.records
                for record in records:
                    if undo:
                        Operation.undo(record, self._engine, delta)
                    else:
                        Operation.redo(record, self._engine, delta)
                signals = (
                    tuple(item.inverse for item in reversed(entry.signals))
                    if undo
                    else tuple(item.forward for item in entry.signals)
                )
                signals = deepcopy(signals)
                next_state = replace(
                    state,
                    cursor=state.cursor + (-1 if undo else 1),
                    revision=state.revision + 1,
                    open_entry=None,
                    intervening=frozenset(),
                )
            except StoreOperationError as error:
                return self._failure(error, StoreErrorStage.VALIDATE)
            try:
                result = self._engine.restore(
                    delta,
                    transaction_id=transaction_id,
                    validate=lambda delta: self._instructions.validate_signals(
                        signals, delta
                    ),
                )
                if not result.success:
                    return result
                self._history.state = next_state
                commit = replace(
                    cast(StoreCommit, result.commit),
                    signals=signals,
                    kind=kind,
                    origin_client_id=origin_client_id,
                    history_entry_id=entry.id,
                    history_group=entry.group,
                    history_label=entry.label,
                )
                return replace(
                    result, commit=self._publish_transaction(commit, True)
                )
            except Exception:
                logger.exception(
                    "History navigation crashed id=%s", transaction_id
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
        with self._lock:
            self._require_booted()
            token = object()
            self._commit_listeners[token] = callback

            logger.debug(
                "Commit listener added listeners=%d",
                len(self._commit_listeners),
            )

        def unsubscribe() -> None:
            with self._lock:
                self._commit_listeners.pop(token, None)
                logger.debug(
                    "Commit listener removed listeners=%d",
                    len(self._commit_listeners),
                )

        return unsubscribe

    def subscribe_history(
        self, callback: Callable[[HistoryStatus], None]
    ) -> Callable[[], None]:
        with self._lock:
            self._require_booted()
            token = object()
            self._history_listeners[token] = callback

        def unsubscribe() -> None:
            with self._lock:
                self._history_listeners.pop(token, None)

        return unsubscribe

    def _publish_transaction(
        self, commit: StoreCommit, history_changed: bool
    ) -> StoreCommit:
        self._publishing_commits = True
        try:
            stamped = self._record_commit(commit)
            if history_changed:
                self._publish_history()
            return stamped
        finally:
            self._publishing_commits = False

    def _publish_history(self) -> None:
        status = self._history.status()
        for token in tuple(self._history_listeners):
            callback = self._history_listeners.get(token)
            if callback is not None:
                try:
                    callback(deepcopy(status))
                except Exception:
                    logger.exception("Store history callback failed")

    def _publish_commit(self, commit: StoreCommit) -> None:
        for token in tuple(self._commit_listeners):
            callback = self._commit_listeners.get(token)
            if callback is not None:
                try:
                    callback(deepcopy(commit))
                except Exception:
                    logger.exception(
                        "Store commit callback failed transaction=%s",
                        commit.transaction_id,
                    )

    def schema_catalog(self) -> dict[str, Any]:
        """Detached declarative metadata for diagnostics."""
        with self._lock:
            self._require_booted()
            result: dict[str, Any] = {}
            for name, node in self._engine.metadata.nodes_by_schema.items():
                schema = node.schema
                result[name] = {
                    "name": name,
                    "kind": "many"
                    if isinstance(schema, ManySchema)
                    else "single",
                    "address": node.address.parts,
                    "persistent": schema.persistent,
                    "track_history": schema.track_history,
                    "fields": {
                        key: {
                            "name": key,
                            "type": type(value).__name__,
                            **deepcopy(value.metadata),
                        }
                        for key, value in schema.fields().items()
                    },
                    "signals": {
                        key: {"name": key, "undo": value.undo}
                        for key, value in schema.signals().items()
                    },
                    "indexes": tuple(sorted(schema.index_keys())),
                    "uniques": tuple(sorted(schema.unique_keys())),
                }
            return result

    def checkpoint(
        self, schemas: tuple[str, ...] | None = None
    ) -> dict[str, Any]:
        with self._lock:
            self._require_booted()
            selected = (
                schemas
                if schemas is not None
                else tuple(self._engine.metadata.nodes_by_schema)
            )
            nodes = {
                name: self._engine.get_node_by_schema(name)
                for name in selected
            }
            return {
                "store_instance_id": self.instance_id,
                "schema_revisions": {
                    name: node.revision for name, node in nodes.items()
                },
                "schemas": {
                    name: node.snapshot() for name, node in nodes.items()
                },
                "history": self._history.status(),
            }

    def observe_checkpoint(
        self,
        callback: Callable[[StoreCommit], None],
        schemas: tuple[str, ...] | None = None,
    ) -> tuple[dict[str, Any], Callable[[], None]]:
        """Atomically take a baseline and subscribe to later commits."""
        with self._lock:
            checkpoint = self.checkpoint(schemas)
            unsubscribe = self.subscribe_commits(callback)
            return checkpoint, unsubscribe

    # -------------------------------------------------
    # Revisions
    # -------------------------------------------------

    def _record_commit(self, commit: StoreCommit) -> StoreCommit:
        """Stamp and publish one successful Store commit."""
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

        if stamped.events or stamped.signals:
            self._publish_commit(stamped)

        return stamped
