# kivyfn/store/server/server.py
from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import (
    TYPE_CHECKING,
    Any,
    Generic,
    TypeVar,
)

from kivyfn.logging import get_logger
from kivyfn.store.address import Address
from kivyfn.store.core.engine import Engine
from kivyfn.store.core.execution import ExecutionResult
from kivyfn.store.operations import OpCode
from kivyfn.store.query import Query, QueryResult
from kivyfn.store.schema import ManySchema
from kivyfn.store.server.query import (
    QuerySubscribeResult,
    QueryUnsubscribeResult,
    RemoteQuerySubscriptionManager,
)
from kivyfn.store.server.revision import StoreRevisionTracker
from kivyfn.store.server.signal import (
    RemoteSignalManager,
    SignalBindResult,
    SignalDispatchMessage,
    SignalRequest,
    SignalUnbindResult,
)
from kivyfn.store.server.subscription import (
    DispatchMessage,
    RemoteSubscriptionManager,
    SubscribeResult,
    UnsubscribeResult,
)
from kivyfn.store.server.transport import StoreServerTransport
from kivyfn.store.storage import AsyncPersistenceCoordinator


if TYPE_CHECKING:
    from kivyfn.store.builder.config import StoreConfig
    from kivyfn.store.events.event import StoreEvent
    from kivyfn.store.node import Node
    from kivyfn.store.server.proxy import StoreServerProxy


ResultT = TypeVar("ResultT")


logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class StoreExecutionResult(Generic[ResultT]):
    """
    Result of a server operation that may generate subscription events.

    `result` contains the direct store-domain result of the operation.

    `dispatches` contains transport-independent subscription messages
    produced from the operation's events. The active server transport is
    responsible for delivering those messages through its own channel
    mechanism.
    """

    result: ResultT
    dispatches: tuple[DispatchMessage, ...]
    signal_dispatches: tuple[SignalDispatchMessage, ...] = ()


class StoreServer:
    """
    Transport-independent server for a remotely accessible store.

    StoreServer owns:

    - the Engine
    - server-side subscription management
    - server-side signal management
    - one selected server transport
    - one restricted transport-facing StoreServerProxy
    - the complete server lifecycle

    Public operation methods accept concrete Python arguments. The selected
    transport is responsible for parsing its wire protocol before calling
    the proxy and for serializing returned values afterward.

    StoreServer contains no Socket.IO, aiohttp, Unix-socket, TCP, framing,
    payload-parsing, room, or connection implementation details.
    """

    def __init__(
        self,
        root: Node,
        config: StoreConfig,
        transport: StoreServerTransport,
    ) -> None:
        self._engine = Engine(
            root=root,
            config=config,
        )

        self._subscriptions = RemoteSubscriptionManager(
            self._engine,
        )

        self._signals = RemoteSignalManager(
            self._engine,
        )

        self._revisions = StoreRevisionTracker()

        self._queries = RemoteQuerySubscriptionManager(
            self._engine,
            self._revisions,
        )

        self._persistence = AsyncPersistenceCoordinator(
            self._engine,
        )

        self._synchronization_lock = RLock()
        self._transport = transport

        from kivyfn.store.server.proxy import StoreServerProxy

        self._proxy = StoreServerProxy(
            self,
        )

        self._running = False

    # -------------------------------------------------
    # Lifecycle
    # -------------------------------------------------

    async def start(self) -> None:
        """
        Boot the owned Engine and start the selected transport.

        Calling start() while already running is safe.

        An Engine may only be booted once, so its own `booted` state is
        checked before calling boot().
        """
        if self._running:
            return

        if not self._engine.booted:
            self._engine.boot()

        self._persistence.start()

        try:
            await self._transport.start(
                self._proxy,
            )
        except BaseException:
            await self._persistence.stop(
                flush=True,
            )
            raise

        self._running = True

    async def stop(self) -> None:
        """
        Stop the selected transport.

        Calling stop() while already stopped is safe. The Engine remains
        booted, allowing the transport to be started again on the same
        server instance.
        """
        if not self._running:
            if self._engine.has_pending_persistence:
                await self._persistence.stop(
                    flush=True,
                )

            return

        self._running = False

        try:
            await self._transport.stop()
        finally:
            await self._persistence.stop(
                flush=True,
            )

    # -------------------------------------------------
    # Reads
    # -------------------------------------------------

    def schema_info(
        self,
        schema_name: str,
    ) -> dict[str, Any]:
        """Return transport-safe metadata for one registered schema."""
        with self._synchronization_lock:
            node = self._engine.metadata.get_node_by_schema(
                schema_name,
            )

            schema = node.schema

            return {
                "name": schema.name,
                "address": node.address.to_list(),
                "many": isinstance(
                    schema,
                    ManySchema,
                ),
                "fields": list(
                    schema.fields()
                ),
                "signals": list(
                    schema.signals()
                ),
            }

    def snapshot(
        self,
        address: Address[Any],
        id: str | None = None,
        key: str | None = None,
        *,
        resolve_foreignkeys: bool = False,
    ) -> Any:
        """
        Return a snapshot from a store node.

        The address and optional selectors must already have been parsed by
        the active transport.
        """
        return self._engine.snapshot(
            address=address,
            id=id,
            key=key,
            resolve_foreignkeys=resolve_foreignkeys,
        )

    def sync_snapshot(
        self,
        address: Address[Any],
        *,
        resolve_foreignkeys: bool = False,
    ) -> dict[str, Any]:
        """Return an authoritative snapshot with revision metadata."""
        with self._synchronization_lock:
            data = self._engine.snapshot(
                address=address,
                resolve_foreignkeys=resolve_foreignkeys,
            )

            return self._revisions.snapshot(
                address,
                data,
            )

    def revision_audit(
        self,
        addresses: list[Address[Any]],
    ) -> dict[str, Any]:
        """Return lightweight revision metadata for store addresses."""
        with self._synchronization_lock:
            return self._revisions.report(
                addresses,
            )

    def lookup(
        self,
        address: Address[Any],
        fields: str | tuple[str, ...],
        value: Any,
        *,
        first: bool = False,
    ) -> Any:
        """Look up rows whose selected fields match the supplied value."""
        return self._engine.lookup(
            address=address,
            fields=fields,
            value=value,
            first=first,
        )

    def to_dict(
        self,
        address: Address[Any],
    ) -> Any:
        """Return the complete value of the requested store node."""
        return self._engine.to_dict(
            address,
        )

    def fetch(
        self,
        query: Query[Any],
    ) -> QueryResult:
        with self._synchronization_lock:
            return self._queries.fetch(
                query,
            )

    # -------------------------------------------------
    # Query subscriptions
    # -------------------------------------------------

    def subscribe_query(
        self,
        client_id: str,
        query: Query[Any],
    ) -> QuerySubscribeResult:
        with self._synchronization_lock:
            return self._queries.subscribe(
                client_id,
                query,
            )

    def unsubscribe_query(
        self,
        client_id: str,
        subscription_token: str,
    ) -> QueryUnsubscribeResult:
        with self._synchronization_lock:
            return self._queries.unsubscribe(
                client_id,
                subscription_token,
            )

    def sync_query(
        self,
        client_id: str,
        subscription_token: str,
    ) -> dict[str, Any]:
        with self._synchronization_lock:
            return self._queries.sync_snapshot(
                client_id,
                subscription_token,
            ).payload()

    def audit_queries(
        self,
        client_id: str,
        subscription_tokens: list[str],
    ) -> dict[str, Any]:
        with self._synchronization_lock:
            return self._queries.audit(
                client_id,
                subscription_tokens,
            )

    # -------------------------------------------------
    # Subscriptions
    # -------------------------------------------------

    def subscribe(
        self,
        client_id: str,
        opcode: OpCode,
        address: Address[Any],
        *,
        id: str | None = None,
        key: str | None = None,
        depth: int | None = 0,
        resolve_foreignkeys: bool = False,
        propagate_foreignkeys: bool = False,
    ) -> SubscribeResult:
        """
        Register an event subscription for a remote client.

        The result tells the transport whether the client must join the
        subscription's generic event channel.
        """
        return self._subscriptions.subscribe(
            client_id=client_id,
            opcode=opcode,
            address=address,
            id=id,
            key=key,
            depth=depth,
            resolve_foreignkeys=resolve_foreignkeys,
            propagate_foreignkeys=propagate_foreignkeys,
        )

    def unsubscribe(
        self,
        client_id: str,
        subscription_token: str,
    ) -> UnsubscribeResult:
        """
        Remove one event subscription owned by a remote client.

        The result tells the transport whether the client may leave the
        subscription's generic event channel.
        """
        return self._subscriptions.unsubscribe(
            client_id=client_id,
            token=subscription_token,
        )

    # -------------------------------------------------
    # Signals
    # -------------------------------------------------

    def bind_signal(
        self,
        client_id: str,
        address: Address[Any],
        signal: str,
        *,
        id: str | None = None,
    ) -> SignalBindResult:
        """Bind a remote client to a store signal stream."""
        return self._signals.bind(
            client_id=client_id,
            address=address,
            signal=signal,
            id=id,
        )

    def unbind_signal(
        self,
        client_id: str,
        binding_token: str,
    ) -> SignalUnbindResult:
        """Remove one signal binding owned by a remote client."""
        return self._signals.unbind(
            client_id=client_id,
            token=binding_token,
        )

    def emit_signal(
        self,
        address: Address[Any],
        signal: str,
        data: Any = None,
        *,
        id: str | None = None,
    ) -> tuple[SignalDispatchMessage, ...]:
        """
        Emit a store signal.

        Returns transport-independent dispatch messages. The transport is
        responsible for delivering each message to its generic channel.
        """
        return self._signals.emit(
            address=address,
            signal=signal,
            data=data,
            id=id,
        )

    # -------------------------------------------------
    # Transactions
    # -------------------------------------------------

    def transaction(
        self,
        instructions: list[dict[str, Any]],
        signals: tuple[SignalRequest, ...] = (),
    ) -> StoreExecutionResult[ExecutionResult]:
        """
        Execute one atomic state transaction.

        Signal targets are validated before canonical mutations execute.
        Signal occurrences are generated only after a successful commit.
        """
        with self._synchronization_lock:
            for request in signals:
                self._signals.validate(
                    request.address,
                    request.signal,
                    id=request.id,
                )

            result = self._engine.execute(
                instructions,
            )

            signal_dispatches: list[
                SignalDispatchMessage
            ] = []

            if result.success:
                for request in signals:
                    signal_dispatches.extend(
                        self._signals.emit(
                            request.address,
                            request.signal,
                            request.data,
                            id=request.id,
                        )
                    )

            return StoreExecutionResult(
                result=result,
                dispatches=self._dispatch_events(
                    result.events,
                ),
                signal_dispatches=tuple(
                    signal_dispatches,
                ),
            )

    # -------------------------------------------------
    # Undo / redo
    # -------------------------------------------------

    def undo(
        self,
    ) -> StoreExecutionResult[list[StoreEvent]]:
        """
        Undo the latest successful transaction.

        Engine.undo() directly returns the list of events produced by the
        inverse operations.
        """
        with self._synchronization_lock:
            events = self._engine.undo()

            return StoreExecutionResult(
                result=events,
                dispatches=self._dispatch_events(
                    events,
                ),
            )

    def redo(
        self,
    ) -> StoreExecutionResult[list[StoreEvent]]:
        """
        Reapply the latest undone transaction.

        Engine.redo() directly returns the list of events produced by the
        reapplied operations.
        """
        with self._synchronization_lock:
            events = self._engine.redo()

            return StoreExecutionResult(
                result=events,
                dispatches=self._dispatch_events(
                    events,
                ),
            )

    # -------------------------------------------------
    # Client lifecycle
    # -------------------------------------------------

    def disconnect_client(
        self,
        client_id: str,
    ) -> None:
        """
        Remove all subscriptions and signal bindings owned by a client.

        Detecting a disconnection belongs to the transport. Cleaning
        associated store state belongs to StoreServer.
        """
        self._subscriptions.remove_client(
            client_id,
        )

        self._queries.remove_client(
            client_id,
        )

        self._signals.remove_client(
            client_id,
        )

    # -------------------------------------------------
    # Event dispatch
    # -------------------------------------------------

    def _dispatch_events(
        self,
        events: list[StoreEvent],
    ) -> tuple[DispatchMessage, ...]:
        """Create revisioned messages for successful store events."""
        if not events:
            return ()

        dispatches: list[DispatchMessage] = []
        revisioned_events: list[StoreEvent] = []

        for event in events:
            revision = self._revisions.advance(
                event.address,
            )

            revisioned = event.copy_with(
                server_instance_id=self._revisions.instance_id,
                revision=revision,
            )

            revisioned_events.append(
                revisioned,
            )

            try:
                dispatches.extend(
                    self._subscriptions.dispatch(
                        revisioned,
                    )
                )
            except Exception:
                logger.exception(
                    (
                        "Store subscription dispatch failed for %s "
                        "at revision %d; revision auditing will "
                        "reconcile remote live-state clients."
                    ),
                    event.address,
                    revision,
                )

        try:
            dispatches.extend(
                self._queries.dispatch(
                    revisioned_events,
                )
            )
        except Exception:
            logger.exception(
                (
                    "Query subscription dispatch failed; query "
                    "auditing will reconcile remote query "
                    "subscriptions."
                )
            )

        return tuple(
            dispatches,
        )

    # -------------------------------------------------
    # State
    # -------------------------------------------------

    @property
    def booted(self) -> bool:
        """Whether the owned Engine has completed booting."""
        return self._engine.booted

    @property
    def running(self) -> bool:
        """Whether this server's selected transport is running."""
        return self._running

    @property
    def can_undo(self) -> bool:
        """Whether the Engine has a transaction available to undo."""
        return self._engine.can_undo

    @property
    def can_redo(self) -> bool:
        """Whether the Engine has a transaction available to redo."""
        return self._engine.can_redo

    # -------------------------------------------------
    # Owned components
    # -------------------------------------------------

    @property
    def engine(self) -> Engine:
        """Return the Engine owned by this server."""
        return self._engine

    @property
    def subscription(
        self,
    ) -> RemoteSubscriptionManager:
        """Return the server-side subscription manager."""
        return self._subscriptions

    @property
    def query(
        self,
    ) -> RemoteQuerySubscriptionManager:
        """Return the server-side query subscription manager."""
        return self._queries

    @property
    def signal(self) -> RemoteSignalManager:
        """Return the server-side signal manager."""
        return self._signals

    @property
    def transport(self) -> StoreServerTransport:
        """Return the selected server transport."""
        return self._transport

    @property
    def proxy(self) -> StoreServerProxy:
        """Return the restricted transport-facing server proxy."""
        return self._proxy
