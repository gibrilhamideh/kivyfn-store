# kivyfn/store/server/proxy.py
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kivyfn.store.address import Address
from kivyfn.store.operations import OpCode
from kivyfn.store.query import Query, QueryResult
from kivyfn.store.server.query import (
    QuerySubscribeResult,
    QueryUnsubscribeResult,
)
from kivyfn.store.server.signal import (
    SignalBindResult,
    SignalDispatchMessage,
    SignalRequest,
    SignalUnbindResult,
)
from kivyfn.store.server.subscription import (
    SubscribeResult,
    UnsubscribeResult,
)


if TYPE_CHECKING:
    from kivyfn.store.core.execution import ExecutionResult
    from kivyfn.store.events.event import StoreEvent
    from kivyfn.store.server.server import (
        StoreExecutionResult,
        StoreServer,
    )


class StoreServerProxy:
    """
    Restricted transport-facing proxy for StoreServer.

    The proxy exposes only the concrete store operations required by server
    transports. It prevents a transport from accessing:

    - the Engine directly
    - server lifecycle methods
    - server-side managers
    - the selected transport
    - internal server state

    The proxy performs no parsing, validation, serialization, networking, or
    transport-specific routing. A transport must parse its own wire protocol
    and pass concrete Python arguments to these methods.
    """

    __slots__ = (
        "_server",
    )

    def __init__(
        self,
        server: StoreServer,
    ) -> None:
        self._server = server

    # -------------------------------------------------
    # Reads
    # -------------------------------------------------

    def schema_info(self, schema_name: str) -> dict[str, Any]:
        return self._server.schema_info(schema_name)

    def snapshot(
        self,
        address: Address[Any],
        id: str | None = None,
        key: str | None = None,
        *,
        resolve_foreignkeys: bool = False,
    ) -> Any:
        """
        Return a snapshot from the requested store address.
        """
        return self._server.snapshot(
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
        return self._server.sync_snapshot(
            address=address,
            resolve_foreignkeys=resolve_foreignkeys,
        )

    def revision_audit(
        self,
        addresses: list[Address[Any]],
    ) -> dict[str, Any]:
        """Return revision metadata for selected store addresses."""
        return self._server.revision_audit(
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
        """
        Return rows whose selected fields match the supplied value.
        """
        return self._server.lookup(
            address=address,
            fields=fields,
            value=value,
            first=first,
        )

    def to_dict(
        self,
        address: Address[Any],
    ) -> Any:
        """
        Return the complete value of the requested store node.
        """
        return self._server.to_dict(
            address,
        )

    def fetch(
        self,
        query: Query,
    ) -> QueryResult:
        return self._server.fetch(query)

    # -------------------------------------------------
    # Query subscriptions
    # -------------------------------------------------

    def subscribe_query(
        self,
        client_id: str,
        query: Query,
    ) -> QuerySubscribeResult:
        return self._server.subscribe_query(client_id, query)

    def unsubscribe_query(
        self,
        client_id: str,
        subscription_token: str,
    ) -> QueryUnsubscribeResult:
        return self._server.unsubscribe_query(
            client_id,
            subscription_token,
        )

    def sync_query(
        self,
        client_id: str,
        subscription_token: str,
    ) -> dict[str, Any]:
        return self._server.sync_query(
            client_id,
            subscription_token,
        )

    def audit_queries(
        self,
        client_id: str,
        subscription_tokens: list[str],
    ) -> dict[str, Any]:
        return self._server.audit_queries(
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
        """
        return self._server.subscribe(
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
        Remove an event subscription owned by a remote client.
        """
        return self._server.unsubscribe(
            client_id=client_id,
            subscription_token=subscription_token,
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
        """
        Bind a remote client to a store signal stream.
        """
        return self._server.bind_signal(
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
        """
        Remove a signal binding owned by a remote client.
        """
        return self._server.unbind_signal(
            client_id=client_id,
            binding_token=binding_token,
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
        Emit a store signal and return its outgoing dispatch messages.
        """
        return self._server.emit_signal(
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
        """Execute one transaction and return all dispatch messages."""
        return self._server.transaction(
            instructions,
            signals,
        )

    # -------------------------------------------------
    # Undo / redo
    # -------------------------------------------------

    def undo(
        self,
    ) -> StoreExecutionResult[list[StoreEvent]]:
        """
        Undo the latest reversible transaction.
        """
        return self._server.undo()

    def redo(
        self,
    ) -> StoreExecutionResult[list[StoreEvent]]:
        """
        Reapply the latest undone transaction.
        """
        return self._server.redo()

    # -------------------------------------------------
    # Client lifecycle
    # -------------------------------------------------

    def disconnect_client(
        self,
        client_id: str,
    ) -> None:
        """
        Remove all subscriptions and signal bindings owned by a client.
        """
        self._server.disconnect_client(
            client_id,
        )
