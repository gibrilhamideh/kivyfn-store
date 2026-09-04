# kivyfn/store/local/store.py
from __future__ import annotations

from threading import RLock
from typing import TYPE_CHECKING, Any, TypeVar

from kivyfn.store.address import Address
from kivyfn.store.core.engine import Engine
from kivyfn.store.error import (
    StoreOperationError,
    StoreStateError,
)
from kivyfn.store.local.query import (
    LocalQuerySubscription,
    LocalQuerySubscriptionManager,
    QueryCallback,
)
from kivyfn.store.local.signal import LocalSignalManager
from kivyfn.store.local.subscription import LocalSubscriptionManager
from kivyfn.store.query import Query, QueryResult
from kivyfn.store.response import (
    Response,
    failure,
    success,
)
from kivyfn.store.storage import ThreadPersistenceCoordinator
from kivyfn.store.transaction import (
    Transaction,
    TransactionInput,
    TransactionResult,
    resolve_commands,
)


if TYPE_CHECKING:
    from kivyfn.store.builder import StoreConfig
    from kivyfn.store.events.event import StoreEvent
    from kivyfn.store.node import Node


T = TypeVar("T")


class LocalStore:
    """
    Synchronous, in-process store.

    LocalStore is intended to remain alive for the lifetime of its owning
    application or service.

    Lifecycle:

        store.boot()
        ...
        store.shutdown()

    LocalStore owns:

    - one synchronous Engine;
    - local event subscriptions;
    - local signal bindings;
    - lifecycle state.

    Public operations are serialized through a reentrant lock. This allows
    access from multiple threads while permitting callbacks to re-enter the
    store from the same thread.

    Expected store-operation failures are returned as FailureResponse values.
    Lifecycle and unexpected runtime failures remain exceptions.
    """

    def __init__(
        self,
        root: Node,
        config: StoreConfig,
    ) -> None:
        self._engine = Engine(
            root=root,
            config=config,
        )

        self._subscriptions = LocalSubscriptionManager(
            self._engine,
        )

        self._signals = LocalSignalManager(
            self._engine,
        )

        self._queries = LocalQuerySubscriptionManager(
            self._engine,
        )
        self._persistence = ThreadPersistenceCoordinator(
            self._engine,
        )

        self._lock = RLock()
        self._booted = False
        self._shutdown = False

    # -------------------------------------------------
    # Lifecycle
    # -------------------------------------------------

    def boot(self) -> None:
        """
        Load, migrate, validate, and initialize the local store.

        A LocalStore instance can only be booted once. After shutdown, create
        a new LocalStore instance rather than attempting to boot it again.
        """
        with self._lock:
            if self._booted:
                raise StoreStateError(
                    "Local store is already booted"
                )

            if self._shutdown:
                raise StoreStateError(
                    "A shut-down local store cannot be booted"
                )

            self._engine.boot()
            self._persistence.start()
            self._booted = True

    def shutdown(self) -> None:
        """
        Shut down the local store and release local runtime state.

        Existing event subscriptions and signal bindings are disposed.
        Calling shutdown repeatedly is safe.
        """
        with self._lock:
            if self._shutdown:
                return

            self._booted = False

            try:
                self._persistence.stop(flush=True)
            except BaseException:
                self._booted = True
                raise

            self._subscriptions.clear()
            self._queries.clear()
            self._signals.clear()
            self._shutdown = True

    def _require_booted(self) -> None:
        """
        Ensure that the store is booted and has not been shut down.
        """
        if self._shutdown:
            raise StoreStateError(
                "Local store has been shut down"
            )

        if not self._booted:
            raise StoreStateError(
                "Local store has not been booted"
            )

    @property
    def booted(self) -> bool:
        """
        Whether the store is currently booted and usable.
        """
        with self._lock:
            return (
                self._booted
                and not self._shutdown
            )

    @property
    def is_shutdown(self) -> bool:
        """
        Whether shutdown permanently closed this LocalStore instance.
        """
        with self._lock:
            return self._shutdown

    # -------------------------------------------------
    # Public managers
    # -------------------------------------------------

    @property
    def subscription(
        self,
    ) -> LocalSubscriptionManager:
        """
        Return the local subscription manager.

        The store must be booted before subscriptions can be created.
        """
        with self._lock:
            self._require_booted()
            return self._subscriptions

    @property
    def signal(
        self,
    ) -> LocalSignalManager:
        """
        Return the local signal manager.

        The store must be booted before signal bindings can be created.
        """
        with self._lock:
            self._require_booted()
            return self._signals

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
    ) -> Response[Any]:
        """
        Return a snapshot of a node, row, or field.

        Foreign keys are returned as stored unless resolve_foreignkeys is true.
        """
        with self._lock:
            self._require_booted()

            try:
                data = self._engine.snapshot(
                    address=address,
                    id=id,
                    key=key,
                    resolve_foreignkeys=resolve_foreignkeys,
                )

            except StoreOperationError as exc:
                return failure(
                    exc.failure(),
                )

            return success(
                data,
            )

    def lookup(
        self,
        address: Address[T],
        fields: str | tuple[str, ...],
        value: Any,
        *,
        first: bool = False,
    ) -> Response[T | dict[str, T] | None]:
        """
        Look up rows using an indexed field or indexed field combination.

        When first is true, return the first matching row or None. Otherwise,
        return matching rows keyed by row ID.
        """
        with self._lock:
            self._require_booted()

            try:
                data = self._engine.lookup(
                    address=address,
                    fields=fields,
                    value=value,
                    first=first,
                )

            except StoreOperationError as exc:
                return failure(
                    exc.failure(),
                )

            return success(
                data,
            )

    def to_dict(
        self,
        address: Address[T],
    ) -> Response[T]:
        """
        Serialize the addressed node into its dictionary representation.
        """
        with self._lock:
            self._require_booted()

            try:
                data = self._engine.to_dict(
                    address,
                )

            except StoreOperationError as exc:
                return failure(
                    exc.failure(),
                )

            return success(
                data,
            )

    def fetch(
        self,
        query: Query,
    ) -> Response[QueryResult]:
        with self._lock:
            self._require_booted()

            try:
                data = self._engine.fetch(query)
            except StoreOperationError as exc:
                return failure(exc.failure())

            return success(data)

    def subscribe(
        self,
        query: Query,
        callback: QueryCallback,
    ) -> LocalQuerySubscription:
        with self._lock:
            self._require_booted()
            return self._queries.subscribe(query, callback)

    # -------------------------------------------------
    # Transactions
    # -------------------------------------------------

    def commit(
        self,
        transaction: TransactionInput,
    ) -> Response[TransactionResult]:
        """
        Commit one atomic transaction synchronously.

        The transaction may be supplied as either:

        - a Transaction instance;
        - a raw list of command payloads.

        Events are dispatched after execution and any immediate persistence.
        Delayed schemas are marked dirty before dispatch. The store lock
        remains held through event delivery so another thread
        cannot interleave a transaction between mutation and notification.

        Expected compilation, delta, and validation failures are returned as
        FailureResponse values. Unexpected execution failures propagate.

        Example:

            transaction = (
                Transaction()
                .setrow(address, row_id, "enabled", True)
                .setrow(address, row_id, "value", 10)
            )

            result = store.commit(transaction)
        """
        with self._lock:
            self._require_booted()

            commands = resolve_commands(
                transaction,
            )

            execution = self._engine.execute(
                commands,
            )

            if not execution.success:
                error = execution.error

                if error is None:
                    raise RuntimeError(
                        "Transaction failed without an error"
                    )

                return failure(
                    error.failure(),
                )

            self._subscriptions.dispatch_many(
                execution.events,
            )
            self._queries.dispatch_many(execution.events)

            result = TransactionResult.from_payload(
                execution.payload(),
            )

            return success(
                result,
            )

    # -------------------------------------------------
    # Undo / Redo
    # -------------------------------------------------

    def undo(
        self,
    ) -> Response[list[StoreEvent]]:
        """
        Undo the latest committed transaction.

        Generated events are dispatched to matching local subscriptions.
        """
        with self._lock:
            self._require_booted()

            try:
                events = self._engine.undo()

            except StoreOperationError as exc:
                return failure(
                    exc.failure(),
                )

            self._subscriptions.dispatch_many(
                events,
            )
            self._queries.dispatch_many(events)

            return success(
                events,
            )

    def redo(
        self,
    ) -> Response[list[StoreEvent]]:
        """
        Redo the latest transaction that was previously undone.

        Generated events are dispatched to matching local subscriptions.
        """
        with self._lock:
            self._require_booted()

            try:
                events = self._engine.redo()

            except StoreOperationError as exc:
                return failure(
                    exc.failure(),
                )

            self._subscriptions.dispatch_many(
                events,
            )
            self._queries.dispatch_many(events)

            return success(
                events,
            )

    @property
    def can_undo(self) -> bool:
        """
        Whether the engine currently has a transaction available to undo.
        """
        with self._lock:
            self._require_booted()
            return self._engine.can_undo

    @property
    def can_redo(self) -> bool:
        """
        Whether the engine currently has a transaction available to redo.
        """
        with self._lock:
            self._require_booted()
            return self._engine.can_redo

    # -------------------------------------------------
    # Internal access
    # -------------------------------------------------

    @property
    def engine(self) -> Engine:
        """
        Expose the underlying engine for framework integrations.

        Normal application code should use LocalStore's public API.
        """
        return self._engine

    # -------------------------------------------------
    # Helpers
    # -------------------------------------------------

    def transaction(
        self,
    ) -> Transaction:
        return Transaction()

