from __future__ import annotations

import asyncio
import weakref

from collections import defaultdict
from collections.abc import Iterable
from typing import (
    TYPE_CHECKING,
    Any,
    Literal,
    TypeVar,
    cast,
    overload,
)

from kivyfn.logging import get_logger
from kivyfn.store.address import Address
from kivyfn.store.error import (
    StoreProtocolError,
    StoreStateError,
)
from kivyfn.store.events.event import (
    InsertEvent,
    RemoveEvent,
    SetEvent,
    SetRowEvent,
    StoreEvent,
    event_from_payload,
)
from kivyfn.store.operations import OpCode
from kivyfn.store.query import (
    Query,
    QueryChangeSet,
    QueryResult,
    query_to_payload,
)
from kivyfn.store.remote.query import (
    QueryCallback,
    QuerySubscriptionSpec,
    QuerySyncCallback,
    RemoteQuerySubscription,
)
from kivyfn.store.remote.signal import (
    RemoteSignalBinding,
    SignalBindingSpec,
    SignalCallback,
)
from kivyfn.store.remote.state import ConnectionState
from kivyfn.store.remote.subscription import (
    DispatchResult,
    EventCallback,
    RemoteSubscription,
    SubscriptionSpec,
)
from kivyfn.store.query.sync import (
    QueryRevisionAudit,
    QuerySyncSnapshot,
)
from kivyfn.store.remote.sync import (
    RevisionAudit,
    SyncSnapshot,
)
from kivyfn.store.remote.transport import (
    LifecycleEvent,
    RemoteClientTransport,
)
from kivyfn.store.response import (
    Response,
    from_payload,
    success,
)
from kivyfn.store.transaction import TransactionResult


if TYPE_CHECKING:
    from kivyfn.store.adapter.protocol import SynchronizingAdapter


logger = get_logger(__name__)


T = TypeVar("T")
RowT = TypeVar("RowT")
SignalT = TypeVar("SignalT")


class RemoteStoreClient:
    """Transport-independent, self-recovering remote-store client."""

    def __init__(
        self,
        transport: RemoteClientTransport,
        *,
        audit_interval: float = 5.0,
        recovery_delay: float = 1.0,
    ) -> None:
        if audit_interval <= 0:
            raise ValueError(
                "Remote store audit interval must be greater than zero"
            )

        if recovery_delay <= 0:
            raise ValueError(
                "Remote store recovery delay must be greater than zero"
            )

        self._transport = transport
        self._transport.set_event_handler(
            self.on_events,
        )
        self._transport.set_signal_handler(
            self.on_signal,
        )
        self._transport.set_lifecycle_handler(
            self._on_transport_lifecycle,
        )

        self._subscription_handles: set[
            RemoteSubscription[Any]
        ] = set()
        self._subscriptions: dict[
            str,
            set[RemoteSubscription[Any]],
        ] = defaultdict(set)
        self._subscriptions_by_token: dict[
            str,
            RemoteSubscription[Any],
        ] = {}

        self._query_handles: set[RemoteQuerySubscription] = set()
        self._query_subscriptions: dict[
            str,
            set[RemoteQuerySubscription],
        ] = defaultdict(set)
        self._query_subscriptions_by_token: dict[
            str,
            RemoteQuerySubscription,
        ] = {}
        self._query_tasks: dict[
            RemoteQuerySubscription,
            asyncio.Task[None],
        ] = {}

        self._signal_handles: set[
            RemoteSignalBinding[Any]
        ] = set()
        self._signal_bindings: dict[
            str,
            set[RemoteSignalBinding[Any]],
        ] = defaultdict(set)
        self._signal_bindings_by_token: dict[
            str,
            RemoteSignalBinding[Any],
        ] = {}

        self._adapters: weakref.WeakSet[
            SynchronizingAdapter
        ] = weakref.WeakSet()
        self._adapter_tasks: dict[
            SynchronizingAdapter,
            asyncio.Task[None],
        ] = {}

        self._audit_interval = float(audit_interval)
        self._recovery_delay = float(recovery_delay)
        self._state = ConnectionState.DISCONNECTED
        self._ready_event = asyncio.Event()
        self._lifecycle_lock = asyncio.Lock()
        self._manual_connecting = False
        self._expected_disconnect = False
        self._recovery_task: asyncio.Task[None] | None = None
        self._audit_task: asyncio.Task[None] | None = None

    # -------------------------------------------------
    # Lifecycle
    # -------------------------------------------------

    async def connect(self) -> None:
        async with self._lifecycle_lock:
            if self.ready:
                return

            if not self.transport_connected:
                self._state = ConnectionState.CONNECTING
                self._manual_connecting = True

                try:
                    await self._transport.connect()
                finally:
                    self._manual_connecting = False

            if not self.transport_connected:
                self._state = ConnectionState.DISCONNECTED
                raise StoreStateError(
                    "Remote store transport did not connect"
                )

            try:
                await self._synchronize()
            except Exception:
                if self.transport_connected:
                    self._schedule_recovery()

                raise

    async def disconnect(self) -> None:
        await self._cancel_recovery_task()
        await self._cancel_audit_task()
        await self._cancel_adapter_tasks()
        await self._cancel_query_tasks()

        async with self._lifecycle_lock:
            if (
                not self.transport_connected
                and self._state is ConnectionState.DISCONNECTED
            ):
                return

            self._expected_disconnect = True
            self._state = ConnectionState.DISCONNECTING
            self._ready_event.clear()
            self._mark_adapters_sync_lost(
                "explicit-disconnect",
            )
            self._mark_queries_sync_lost(
                "explicit-disconnect",
            )

            try:
                if self.transport_connected:
                    await self._transport.disconnect()
            finally:
                self._expected_disconnect = False
                self._clear_remote_routes()
                self._state = ConnectionState.DISCONNECTED

    async def wait_until_ready(self) -> None:
        await self._ready_event.wait()

    def _on_transport_lifecycle(
        self,
        event: LifecycleEvent,
    ) -> None:
        if event == "disconnected":
            self._ready_event.clear()
            self._cancel_audit_now()
            self._clear_remote_routes()
            self._mark_adapters_sync_lost(
                "transport-disconnected",
            )
            self._mark_queries_sync_lost(
                "transport-disconnected",
            )

            if self._state is not ConnectionState.DISCONNECTING:
                self._state = ConnectionState.DISCONNECTED

            if not self._expected_disconnect:
                logger.warning(
                    "Remote store transport disconnected; cached adapter "
                    "data remains available while recovery is pending."
                )

            return

        if self._manual_connecting or self._expected_disconnect:
            return

        self._schedule_recovery()

    def _schedule_recovery(self) -> None:
        task = self._recovery_task

        if task is not None and not task.done():
            return

        self._recovery_task = asyncio.create_task(
            self._recovery_loop(),
            name="remote-store-recovery",
        )

    async def _recovery_loop(self) -> None:
        delay = self._recovery_delay

        while self.transport_connected and not self._expected_disconnect:
            try:
                async with self._lifecycle_lock:
                    if self.ready:
                        return

                    await self._synchronize()

                return

            except asyncio.CancelledError:
                raise

            except Exception:
                logger.exception(
                    "Remote store synchronization failed; retrying."
                )

                if not self.transport_connected:
                    return

                await asyncio.sleep(
                    delay,
                )
                delay = min(delay * 2.0, 5.0)

    async def _synchronize(self) -> None:
        self._state = ConnectionState.SYNCHRONIZING
        self._ready_event.clear()
        await self._cancel_audit_task()
        await self._drop_current_remote_registrations()
        self._clear_remote_routes()

        subscription_regs: list[
            tuple[RemoteSubscription[Any], str, str]
        ] = []
        query_regs: list[
            tuple[RemoteQuerySubscription, str, str]
        ] = []
        signal_regs: list[
            tuple[RemoteSignalBinding[Any], str, str]
        ] = []

        try:
            await self._stage_subscriptions(subscription_regs)
            await self._stage_query_subscriptions(query_regs)
            await self._stage_signal_bindings(signal_regs)
            self._commit_staged_registrations(
                subscription_regs,
                query_regs,
                signal_regs,
            )
        except BaseException:
            self._clear_remote_routes()
            await self._rollback_staged_registrations(
                subscription_regs,
                query_regs,
                signal_regs,
            )
            raise

        await self._recover_query_subscriptions()
        await self._recover_adapters()

        self._state = ConnectionState.READY
        self._ready_event.set()
        self._start_audit_task()

        logger.info(
            "Remote store synchronization recovered: "
            "subscriptions=%d queries=%d signals=%d adapters=%d",
            len(self._subscription_handles),
            len(self._query_handles),
            len(self._signal_handles),
            len(tuple(self._loaded_adapters())),
        )

    async def _cancel_recovery_task(self) -> None:
        task = self._recovery_task
        self._recovery_task = None

        if task is None or task.done():
            return

        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass

    @property
    def transport_connected(self) -> bool:
        return self._transport.connected

    @property
    def connected(self) -> bool:
        return self.transport_connected

    @property
    def ready(self) -> bool:
        return (
            self.transport_connected
            and self._state is ConnectionState.READY
        )

    @property
    def state(self) -> ConnectionState:
        return self._state

    def _require_connected(self) -> None:
        if not self.transport_connected:
            raise StoreStateError(
                "Remote store client is not connected"
            )

    def _require_ready(self) -> None:
        if not self.ready:
            raise StoreStateError(
                "Remote store client is not synchronized"
            )

    # -------------------------------------------------
    # Transport requests
    # -------------------------------------------------

    async def _raw_request(
        self,
        event: str,
        message: Any | None = None,
    ) -> Response[Any]:
        self._require_connected()

        payload = await self._transport.call(
            event,
            message,
        )

        return from_payload(
            payload,
        )

    async def _request(
        self,
        event: str,
        message: Any | None = None,
    ) -> Response[Any]:
        self._require_ready()
        return await self._raw_request(
            event,
            message,
        )

    # -------------------------------------------------
    # Reads and synchronization
    # -------------------------------------------------

    async def snapshot(
        self,
        address: Address[T],
        id: str | None = None,
        key: str | None = None,
        *,
        resolve_foreignkeys: bool = False,
    ) -> Response[Any]:
        return await self._request(
            "snapshot",
            {
                "address": address.to_list(),
                "id": id,
                "key": key,
                "resolve_foreignkeys": resolve_foreignkeys,
            },
        )

    async def sync_snapshot(
        self,
        address: Address[T],
        *,
        resolve_foreignkeys: bool = False,
    ) -> Response[SyncSnapshot[Any]]:
        result = await self._raw_request(
            "sync_snapshot",
            {
                "address": address.to_list(),
                "resolve_foreignkeys": resolve_foreignkeys,
            },
        )

        if result.ok is False:
            return cast(
                Response[SyncSnapshot[Any]],
                result,
            )

        return success(
            SyncSnapshot.from_payload(
                result.data,
            )
        )

    async def revision_audit(
        self,
        addresses: Iterable[Address[Any]],
    ) -> Response[RevisionAudit]:
        values = list(
            dict.fromkeys(addresses),
        )
        result = await self._raw_request(
            "revision_audit",
            {
                "addresses": [
                    address.to_list()
                    for address in values
                ],
            },
        )

        if result.ok is False:
            return cast(
                Response[RevisionAudit],
                result,
            )

        return success(
            RevisionAudit.from_payload(
                result.data,
            )
        )

    async def lookup(
        self,
        address: Address[T],
        fields: str | tuple[str, ...],
        value: Any,
        *,
        first: bool = False,
    ) -> Response[T | dict[str, T] | None]:
        field_names = (
            [fields]
            if isinstance(fields, str)
            else list(fields)
        )
        result = await self._request(
            "lookup",
            {
                "address": address.to_list(),
                "fields": field_names,
                "value": value,
                "first": first,
            },
        )

        return cast(
            Response[T | dict[str, T] | None],
            result,
        )

    async def to_dict(
        self,
        address: Address[T],
    ) -> Response[T]:
        result = await self._request(
            "to_dict",
            {
                "address": address.to_list(),
            },
        )

        return cast(
            Response[T],
            result,
        )

    # -------------------------------------------------
    # Queries
    # -------------------------------------------------

    async def fetch_query(
        self,
        query: Query,
    ) -> Response[QueryResult]:
        result = await self._request(
            "query_fetch",
            {
                "query": query_to_payload(query),
            },
        )

        if result.ok is False:
            return cast(Response[QueryResult], result)

        data = result.data

        if not isinstance(data, dict) or not all(
            isinstance(row_id, str) and isinstance(row, dict)
            for row_id, row in data.items()
        ):
            raise StoreProtocolError(
                "Remote query result is invalid"
            )

        return success(cast(QueryResult, data))

    async def subscribe_query(
        self,
        query: Query,
        callback: QueryCallback,
        *,
        sync_callback: QuerySyncCallback | None = None,
    ) -> Response[RemoteQuerySubscription]:
        self._require_ready()
        spec = QuerySubscriptionSpec(query=query)
        result = await self._raw_request(
            "query_subscribe",
            spec.payload(),
        )

        if result.ok is False:
            return cast(Response[RemoteQuerySubscription], result)

        token, event_token = self._parse_registration(
            result.data,
            token_key="subscription_token",
            operation="query_subscribe",
        )
        subscription = RemoteQuerySubscription(
            client=self,
            token=token,
            event_token=event_token,
            spec=spec,
            callback=callback,
            sync_callback=sync_callback,
        )
        self._query_handles.add(subscription)
        self._register_query_route(subscription)

        try:
            await subscription.resync(
                "initial-load",
                notify=False,
            )
        except BaseException:
            await self.dispose_query_subscription(subscription)
            raise

        return success(subscription)

    async def query_sync_snapshot(
        self,
        subscription: RemoteQuerySubscription,
    ) -> Response[QuerySyncSnapshot]:
        result = await self._raw_request(
            "query_sync",
            {
                "subscription_token": subscription.token,
            },
        )

        if result.ok is False:
            return cast(Response[QuerySyncSnapshot], result)

        return success(
            QuerySyncSnapshot.from_payload(result.data)
        )

    async def query_revision_audit(
        self,
        subscriptions: Iterable[RemoteQuerySubscription],
    ) -> Response[QueryRevisionAudit]:
        result = await self._raw_request(
            "query_audit",
            {
                "subscription_tokens": [
                    subscription.token
                    for subscription in subscriptions
                ],
            },
        )

        if result.ok is False:
            return cast(Response[QueryRevisionAudit], result)

        return success(
            QueryRevisionAudit.from_payload(result.data)
        )

    async def dispose_query_subscription(
        self,
        subscription: RemoteQuerySubscription,
    ) -> Response[bool] | None:
        if subscription.disposed:
            return None

        if subscription not in self._query_handles:
            subscription._mark_disposed()
            return None

        registered = (
            self._query_subscriptions_by_token.get(
                subscription.token
            )
            is subscription
        )

        if self.transport_connected and registered:
            result = await self._raw_request(
                "query_unsubscribe",
                {
                    "subscription_token": subscription.token,
                },
            )

            if result.ok is False:
                return cast(Response[bool], result)

        self._remove_query_handle(subscription)
        return success(True)

    def request_query_restore(
        self,
        subscription: RemoteQuerySubscription,
        reason: str,
    ) -> None:
        subscription.mark_sync_lost(reason)

        if not self.ready:
            return

        task = self._query_tasks.get(subscription)

        if task is not None and not task.done():
            return

        task = asyncio.create_task(
            self._restore_query_subscription(
                subscription,
                reason,
            ),
            name=f"query-restore-{subscription.token}",
        )
        self._query_tasks[subscription] = task
        task.add_done_callback(
            lambda completed, current=subscription: (
                self._query_resync_done(current, completed)
            )
        )

    async def _restore_query_subscription(
        self,
        subscription: RemoteQuerySubscription,
        reason: str,
    ) -> None:
        if (
            subscription.disposed
            or subscription not in self._query_handles
            or not subscription.callback_alive
        ):
            return

        old_token = subscription.token
        result = await self._raw_request(
            "query_subscribe",
            subscription.spec.payload(),
        )

        if result.ok is False:
            raise StoreStateError(
                "Failed to restore query subscription: "
                f"{result.error}"
            )

        token, event_token = self._parse_registration(
            result.data,
            token_key="subscription_token",
            operation="query_subscribe",
        )

        if (
            subscription.disposed
            or subscription not in self._query_handles
            or not subscription.callback_alive
        ):
            await self._best_effort_request(
                "query_unsubscribe",
                {"subscription_token": token},
            )
            return

        if old_token != token:
            await self._best_effort_request(
                "query_unsubscribe",
                {"subscription_token": old_token},
            )

        self._remove_query_route(subscription)
        subscription._replace_remote(token, event_token)

        try:
            self._register_query_route(subscription)
        except BaseException:
            await self._best_effort_request(
                "query_unsubscribe",
                {"subscription_token": token},
            )
            raise

        await subscription.resync(reason)

    def request_query_resync(
        self,
        subscription: RemoteQuerySubscription,
        reason: str,
    ) -> None:
        subscription.mark_sync_lost(reason)

        if not self.ready:
            return

        task = self._query_tasks.get(subscription)

        if task is not None and not task.done():
            return

        task = asyncio.create_task(
            subscription.resync(reason),
            name=f"query-resync-{subscription.token}",
        )
        self._query_tasks[subscription] = task
        task.add_done_callback(
            lambda completed, current=subscription: (
                self._query_resync_done(current, completed)
            )
        )

    def _query_resync_done(
        self,
        subscription: RemoteQuerySubscription,
        task: asyncio.Task[None],
    ) -> None:
        if self._query_tasks.get(subscription) is task:
            self._query_tasks.pop(subscription, None)

        if task.cancelled():
            return

        error = task.exception()

        if error is not None:
            logger.error(
                "Query resynchronization failed: %s",
                error,
                exc_info=(
                    type(error),
                    error,
                    error.__traceback__,
                ),
            )

    # -------------------------------------------------
    # Adapter synchronization
    # -------------------------------------------------

    def register_adapter(
        self,
        adapter: SynchronizingAdapter,
    ) -> None:
        self._adapters.add(
            adapter,
        )

    def unregister_adapter(
        self,
        adapter: SynchronizingAdapter,
    ) -> None:
        self._adapters.discard(
            adapter,
        )
        task = self._adapter_tasks.pop(
            adapter,
            None,
        )

        if task is not None:
            task.cancel()

    def request_adapter_resync(
        self,
        adapter: SynchronizingAdapter,
        reason: str,
    ) -> None:
        adapter.mark_sync_lost(
            reason,
        )

        if not self.ready:
            return

        task = self._adapter_tasks.get(
            adapter,
        )

        if task is not None and not task.done():
            return

        task = asyncio.create_task(
            self._resync_adapter(
                adapter,
                reason,
            ),
            name=f"store-resync-{adapter.address}",
        )
        self._adapter_tasks[adapter] = task
        task.add_done_callback(
            lambda completed, current=adapter: (
                self._adapter_resync_done(
                    current,
                    completed,
                )
            )
        )

    async def _resync_adapter(
        self,
        adapter: SynchronizingAdapter,
        reason: str,
    ) -> None:
        await adapter.resync(
            reason,
        )

    def _adapter_resync_done(
        self,
        adapter: SynchronizingAdapter,
        task: asyncio.Task[None],
    ) -> None:
        current = self._adapter_tasks.get(
            adapter,
        )

        if current is task:
            self._adapter_tasks.pop(
                adapter,
                None,
            )

        if task.cancelled():
            return

        error = task.exception()

        if error is not None:
            logger.error(
                "Adapter resynchronization failed for %s: %s",
                adapter.address,
                error,
                exc_info=(
                    type(error),
                    error,
                    error.__traceback__,
                ),
            )

    def _loaded_adapters(self) -> tuple[SynchronizingAdapter, ...]:
        return tuple(
            adapter
            for adapter in self._adapters
            if adapter.loaded
        )

    def _mark_adapters_sync_lost(
        self,
        reason: str,
    ) -> None:
        for adapter in self._loaded_adapters():
            adapter.mark_sync_lost(
                reason,
            )

    async def _recover_adapters(self) -> None:
        await self._cancel_adapter_tasks()
        adapters = self._loaded_adapters()

        if not adapters:
            return

        for adapter in adapters:
            adapter.mark_sync_lost(
                "connection-recovery",
            )

        await asyncio.gather(
            *(
                adapter.resync(
                    "connection-recovery",
                )
                for adapter in adapters
            )
        )

    async def _cancel_adapter_tasks(self) -> None:
        tasks = tuple(
            self._adapter_tasks.values(),
        )
        self._adapter_tasks.clear()

        for task in tasks:
            task.cancel()

        if tasks:
            await asyncio.gather(
                *tasks,
                return_exceptions=True,
            )

    # -------------------------------------------------
    # Revision audit
    # -------------------------------------------------

    def _start_audit_task(self) -> None:
        task = self._audit_task

        if task is not None and not task.done():
            return

        self._audit_task = asyncio.create_task(
            self._audit_loop(),
            name="remote-store-revision-audit",
        )

    def _cancel_audit_now(self) -> None:
        task = self._audit_task
        self._audit_task = None

        if task is not None:
            task.cancel()

    async def _cancel_audit_task(self) -> None:
        task = self._audit_task
        self._audit_task = None

        if task is None or task.done():
            return

        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _audit_loop(self) -> None:
        failure_logged = False

        while True:
            await asyncio.sleep(self._audit_interval)

            if not self.ready:
                continue

            adapters = self._loaded_adapters()
            queries = tuple(
                subscription
                for subscription in self._query_handles
                if not subscription.disposed
                and subscription.callback_alive
            )

            if not adapters and not queries:
                continue

            try:
                if adapters:
                    await self._audit_adapters(adapters)

                if queries:
                    await self._audit_queries(queries)
            except asyncio.CancelledError:
                raise
            except Exception:
                if not failure_logged:
                    logger.exception(
                        "Remote store revision audit failed."
                    )

                failure_logged = True
            else:
                if failure_logged:
                    logger.info(
                        "Remote store revision audit recovered."
                    )

                failure_logged = False

    async def _audit_adapters(
        self,
        adapters: tuple[SynchronizingAdapter, ...],
    ) -> None:
        response = await self.revision_audit(
            adapter.address
            for adapter in adapters
        )

        if response.ok is False:
            raise StoreStateError(
                "Revision audit was rejected: "
                f"{response.error}"
            )

        audit = response.data

        for adapter in adapters:
            expected = audit.revisions.get(adapter.address)

            if expected is None:
                self.request_adapter_resync(
                    adapter,
                    "audit-address-missing",
                )
                continue

            if (
                not adapter.synchronized
                or adapter.server_instance_id
                != audit.server_instance_id
                or adapter.revision != expected
            ):
                self.request_adapter_resync(
                    adapter,
                    "revision-audit-mismatch",
                )

    async def _audit_queries(
        self,
        subscriptions: tuple[RemoteQuerySubscription, ...],
    ) -> None:
        response = await self.query_revision_audit(subscriptions)

        if response.ok is False:
            raise StoreStateError(
                "Query audit was rejected: "
                f"{response.error}"
            )

        audit = response.data

        for subscription in subscriptions:
            entry = audit.entries.get(subscription.token)

            if entry is None:
                self.request_query_restore(
                    subscription,
                    "query-audit-token-missing",
                )
                continue

            if not subscription.audit_matches(
                audit.server_instance_id,
                entry.sequence,
                entry.dirty,
            ):
                self.request_query_resync(
                    subscription,
                    "query-audit-mismatch",
                )

    # -------------------------------------------------
    # Subscriptions
    # -------------------------------------------------

    @overload
    async def subscribe(
        self,
        opcode: Literal[OpCode.SET],
        address: Address[Any],
        callback: EventCallback[SetEvent],
        *,
        id: None = None,
        key: str | None = None,
        depth: int | None = 0,
        resolve_foreignkeys: bool = False,
        propagate_foreignkeys: bool = False,
    ) -> Response[RemoteSubscription[SetEvent]]:
        ...

    @overload
    async def subscribe(
        self,
        opcode: Literal[OpCode.SETROW],
        address: Address[Any],
        callback: EventCallback[SetRowEvent],
        *,
        id: str | None = None,
        key: str | None = None,
        depth: int | None = 0,
        resolve_foreignkeys: bool = False,
        propagate_foreignkeys: bool = False,
    ) -> Response[RemoteSubscription[SetRowEvent]]:
        ...

    @overload
    async def subscribe(
        self,
        opcode: Literal[OpCode.INSERT],
        address: Address[Any],
        callback: EventCallback[InsertEvent[RowT]],
        *,
        id: None = None,
        key: None = None,
        depth: int | None = 0,
        resolve_foreignkeys: bool = False,
        propagate_foreignkeys: bool = False,
    ) -> Response[RemoteSubscription[InsertEvent[RowT]]]:
        ...

    @overload
    async def subscribe(
        self,
        opcode: Literal[OpCode.REMOVE],
        address: Address[Any],
        callback: EventCallback[RemoveEvent[RowT]],
        *,
        id: None = None,
        key: None = None,
        depth: int | None = 0,
        resolve_foreignkeys: bool = False,
        propagate_foreignkeys: bool = False,
    ) -> Response[RemoteSubscription[RemoveEvent[RowT]]]:
        ...

    async def subscribe(
        self,
        opcode: OpCode,
        address: Address[Any],
        callback: EventCallback[Any],
        *,
        id: str | None = None,
        key: str | None = None,
        depth: int | None = 0,
        resolve_foreignkeys: bool = False,
        propagate_foreignkeys: bool = False,
    ) -> Response[RemoteSubscription[Any]]:
        self._require_ready()
        spec = SubscriptionSpec(
            opcode=opcode,
            address=address,
            id=id,
            key=key,
            depth=depth,
            resolve_foreignkeys=resolve_foreignkeys,
            propagate_foreignkeys=propagate_foreignkeys,
        )
        result = await self._raw_request(
            "subscribe",
            spec.payload(),
        )

        if result.ok is False:
            return cast(
                Response[RemoteSubscription[Any]],
                result,
            )

        token, event_token = self._parse_registration(
            result.data,
            token_key="subscription_token",
            operation="subscribe",
        )
        subscription: RemoteSubscription[Any] = RemoteSubscription(
            client=self,
            token=token,
            event_token=event_token,
            spec=spec,
            callback=callback,
        )
        self._subscription_handles.add(
            subscription,
        )
        self._register_subscription_route(
            subscription,
        )

        return success(
            subscription,
        )

    async def dispose_subscription(
        self,
        subscription: RemoteSubscription[Any],
    ) -> Response[bool] | None:
        if subscription.disposed:
            return None

        if subscription not in self._subscription_handles:
            subscription._mark_disposed()
            return None

        registered = (
            self._subscriptions_by_token.get(
                subscription.token,
            )
            is subscription
        )

        if self.transport_connected and registered:
            result = await self._raw_request(
                "unsubscribe",
                {
                    "subscription_token": subscription.token,
                },
            )

            if result.ok is False:
                return cast(
                    Response[bool],
                    result,
                )

        self._remove_subscription_handle(
            subscription,
        )

        return success(
            True,
        )

    async def unsubscribe(
        self,
        token: str,
    ) -> Response[bool]:
        subscription = next(
            (
                current
                for current in self._subscription_handles
                if current.token == token
            ),
            None,
        )

        if subscription is not None:
            result = await self.dispose_subscription(
                subscription,
            )

            if result is None:
                return success(
                    True,
                )

            return result

        result = await self._request(
            "unsubscribe",
            {
                "subscription_token": token,
            },
        )

        return cast(
            Response[bool],
            result,
        )

    async def _stage_subscriptions(
        self,
        staged: list[tuple[RemoteSubscription[Any], str, str]],
    ) -> None:
        for subscription in tuple(self._subscription_handles):
            if subscription.disposed or not subscription.callback_alive:
                self._remove_subscription_handle(
                    subscription,
                )
                continue

            result = await self._raw_request(
                "subscribe",
                subscription.spec.payload(),
            )

            if result.ok is False:
                raise StoreStateError(
                    "Failed to restore subscription for "
                    f"{subscription.address}: {result.error}"
                )

            token, event_token = self._parse_registration(
                result.data,
                token_key="subscription_token",
                operation="subscribe",
            )
            staged.append(
                (
                    subscription,
                    token,
                    event_token,
                )
            )

    def _register_subscription_route(
        self,
        subscription: RemoteSubscription[Any],
    ) -> None:
        token = subscription.token

        if token in self._subscriptions_by_token:
            raise StoreProtocolError(
                "Remote server returned a duplicate subscription token: "
                f"{token!r}"
            )

        self._subscriptions[
            subscription.event_token
        ].add(
            subscription,
        )
        self._subscriptions_by_token[token] = subscription

    def _remove_subscription_handle(
        self,
        subscription: RemoteSubscription[Any],
    ) -> None:
        self._subscription_handles.discard(
            subscription,
        )
        self._remove_subscription_route(
            subscription,
        )
        subscription._mark_disposed()

    def _remove_subscription_route(
        self,
        subscription: RemoteSubscription[Any],
    ) -> None:
        self._subscriptions_by_token.pop(
            subscription.token,
            None,
        )
        bucket = self._subscriptions.get(
            subscription.event_token,
        )

        if bucket is None:
            return

        bucket.discard(
            subscription,
        )

        if not bucket:
            self._subscriptions.pop(
                subscription.event_token,
                None,
            )

    # -------------------------------------------------
    # Query registration and recovery
    # -------------------------------------------------

    async def _stage_query_subscriptions(
        self,
        staged: list[
            tuple[RemoteQuerySubscription, str, str]
        ],
    ) -> None:
        for subscription in tuple(self._query_handles):
            if subscription.disposed or not subscription.callback_alive:
                self._remove_query_handle(subscription)
                continue

            result = await self._raw_request(
                "query_subscribe",
                subscription.spec.payload(),
            )

            if result.ok is False:
                raise StoreStateError(
                    "Failed to restore query subscription: "
                    f"{result.error}"
                )

            token, event_token = self._parse_registration(
                result.data,
                token_key="subscription_token",
                operation="query_subscribe",
            )
            staged.append((subscription, token, event_token))

    def _register_query_route(
        self,
        subscription: RemoteQuerySubscription,
    ) -> None:
        token = subscription.token

        if token in self._query_subscriptions_by_token:
            raise StoreProtocolError(
                "Remote server returned a duplicate query token: "
                f"{token!r}"
            )

        self._query_subscriptions[
            subscription.event_token
        ].add(subscription)
        self._query_subscriptions_by_token[token] = subscription

    def _remove_query_handle(
        self,
        subscription: RemoteQuerySubscription,
    ) -> None:
        self._query_handles.discard(subscription)
        self._remove_query_route(subscription)
        task = self._query_tasks.pop(subscription, None)

        if task is not None:
            task.cancel()

        subscription._mark_disposed()

    def _remove_query_route(
        self,
        subscription: RemoteQuerySubscription,
    ) -> None:
        self._query_subscriptions_by_token.pop(
            subscription.token,
            None,
        )
        bucket = self._query_subscriptions.get(
            subscription.event_token
        )

        if bucket is None:
            return

        bucket.discard(subscription)

        if not bucket:
            self._query_subscriptions.pop(
                subscription.event_token,
                None,
            )

    def _mark_queries_sync_lost(self, reason: str) -> None:
        for subscription in tuple(self._query_handles):
            subscription.mark_sync_lost(reason)

    async def _recover_query_subscriptions(self) -> None:
        await self._cancel_query_tasks()
        subscriptions = tuple(
            subscription
            for subscription in self._query_handles
            if not subscription.disposed
            and subscription.callback_alive
        )

        if not subscriptions:
            return

        await asyncio.gather(
            *(
                subscription.resync("connection-recovery")
                for subscription in subscriptions
            )
        )

    async def _cancel_query_tasks(self) -> None:
        tasks = tuple(self._query_tasks.values())
        self._query_tasks.clear()

        for task in tasks:
            task.cancel()

        if tasks:
            await asyncio.gather(
                *tasks,
                return_exceptions=True,
            )

    # -------------------------------------------------
    # Signals
    # -------------------------------------------------

    async def bind_signal(
        self,
        address: Address[Any],
        signal: str,
        callback: SignalCallback[SignalT],
        *,
        id: str | None = None,
    ) -> Response[RemoteSignalBinding[SignalT]]:
        self._require_ready()
        spec = SignalBindingSpec(
            address=address,
            signal=signal,
            id=id,
        )
        result = await self._raw_request(
            "signal_bind",
            spec.payload(),
        )

        if result.ok is False:
            return cast(
                Response[RemoteSignalBinding[SignalT]],
                result,
            )

        token, event_token = self._parse_registration(
            result.data,
            token_key="binding_token",
            operation="signal_bind",
        )
        binding: RemoteSignalBinding[SignalT] = RemoteSignalBinding(
            client=self,
            token=token,
            event_token=event_token,
            spec=spec,
            callback=callback,
        )
        self._signal_handles.add(
            binding,
        )
        self._register_signal_route(
            binding,
        )

        return success(
            binding,
        )

    async def dispose_signal_binding(
        self,
        binding: RemoteSignalBinding[Any],
    ) -> Response[bool] | None:
        if binding.disposed:
            return None

        if binding not in self._signal_handles:
            binding._mark_disposed()
            return None

        registered = (
            self._signal_bindings_by_token.get(
                binding.token,
            )
            is binding
        )

        if self.transport_connected and registered:
            result = await self._raw_request(
                "signal_unbind",
                {
                    "binding_token": binding.token,
                },
            )

            if result.ok is False:
                return cast(
                    Response[bool],
                    result,
                )

        self._remove_signal_handle(
            binding,
        )

        return success(
            True,
        )

    async def request_signal_bind(
        self,
        address: Address[Any],
        signal: str,
        *,
        id: str | None = None,
    ) -> Response[dict[str, Any]]:
        result = await self._request(
            "signal_bind",
            {
                "address": address.to_list(),
                "signal": signal,
                "id": id,
            },
        )

        return cast(
            Response[dict[str, Any]],
            result,
        )

    async def request_signal_unbind(
        self,
        token: str,
    ) -> Response[bool]:
        binding = next(
            (
                current
                for current in self._signal_handles
                if current.token == token
            ),
            None,
        )

        if binding is not None:
            result = await self.dispose_signal_binding(
                binding,
            )

            if result is None:
                return success(
                    True,
                )

            return result

        result = await self._request(
            "signal_unbind",
            {
                "binding_token": token,
            },
        )

        return cast(
            Response[bool],
            result,
        )

    async def emit_signal(
        self,
        address: Address[Any],
        signal: str,
        data: Any = None,
        *,
        id: str | None = None,
    ) -> Response[bool]:
        result = await self._request(
            "signal_emit",
            {
                "address": address.to_list(),
                "signal": signal,
                "id": id,
                "data": data,
            },
        )

        return cast(
            Response[bool],
            result,
        )

    async def _stage_signal_bindings(
        self,
        staged: list[tuple[RemoteSignalBinding[Any], str, str]],
    ) -> None:
        for binding in tuple(self._signal_handles):
            if binding.disposed or not binding.callback_alive:
                self._remove_signal_handle(
                    binding,
                )
                continue

            result = await self._raw_request(
                "signal_bind",
                binding.spec.payload(),
            )

            if result.ok is False:
                raise StoreStateError(
                    "Failed to restore signal binding for "
                    f"{binding.address}: {result.error}"
                )

            token, event_token = self._parse_registration(
                result.data,
                token_key="binding_token",
                operation="signal_bind",
            )
            staged.append(
                (
                    binding,
                    token,
                    event_token,
                )
            )

    def _register_signal_route(
        self,
        binding: RemoteSignalBinding[Any],
    ) -> None:
        token = binding.token

        if token in self._signal_bindings_by_token:
            raise StoreProtocolError(
                "Remote server returned a duplicate signal token: "
                f"{token!r}"
            )

        self._signal_bindings[
            binding.event_token
        ].add(
            binding,
        )
        self._signal_bindings_by_token[token] = binding

    def _remove_signal_handle(
        self,
        binding: RemoteSignalBinding[Any],
    ) -> None:
        self._signal_handles.discard(
            binding,
        )
        self._remove_signal_route(
            binding,
        )
        binding._mark_disposed()

    def _remove_signal_route(
        self,
        binding: RemoteSignalBinding[Any],
    ) -> None:
        self._signal_bindings_by_token.pop(
            binding.token,
            None,
        )
        bucket = self._signal_bindings.get(
            binding.event_token,
        )

        if bucket is None:
            return

        bucket.discard(
            binding,
        )

        if not bucket:
            self._signal_bindings.pop(
                binding.event_token,
                None,
            )

    # -------------------------------------------------
    # Atomic registration recovery
    # -------------------------------------------------

    def _commit_staged_registrations(
        self,
        subscriptions: list[
            tuple[RemoteSubscription[Any], str, str]
        ],
        queries: list[
            tuple[RemoteQuerySubscription, str, str]
        ],
        signals: list[
            tuple[RemoteSignalBinding[Any], str, str]
        ],
    ) -> None:
        subscription_tokens = [
            token
            for _subscription, token, _event_token in subscriptions
        ]
        query_tokens = [
            token
            for _subscription, token, _event_token in queries
        ]
        signal_tokens = [
            token
            for _binding, token, _event_token in signals
        ]

        self._validate_unique_tokens(
            subscription_tokens,
            "subscription",
        )
        self._validate_unique_tokens(query_tokens, "query")
        self._validate_unique_tokens(signal_tokens, "signal")

        for subscription, _token, _event_token in subscriptions:
            if (
                subscription.disposed
                or subscription not in self._subscription_handles
                or not subscription.callback_alive
            ):
                raise StoreStateError(
                    "A subscription became unavailable while the "
                    "remote store was synchronizing"
                )

        for subscription, _token, _event_token in queries:
            if (
                subscription.disposed
                or subscription not in self._query_handles
                or not subscription.callback_alive
            ):
                raise StoreStateError(
                    "A query subscription became unavailable while "
                    "the remote store was synchronizing"
                )

        for binding, _token, _event_token in signals:
            if (
                binding.disposed
                or binding not in self._signal_handles
                or not binding.callback_alive
            ):
                raise StoreStateError(
                    "A signal binding became unavailable while the "
                    "remote store was synchronizing"
                )

        for subscription, token, event_token in subscriptions:
            subscription._replace_remote(token, event_token)
            self._register_subscription_route(subscription)

        for subscription, token, event_token in queries:
            subscription._replace_remote(token, event_token)
            self._register_query_route(subscription)

        for binding, token, event_token in signals:
            binding._replace_remote(token, event_token)
            self._register_signal_route(binding)

    @staticmethod
    def _validate_unique_tokens(
        tokens: list[str],
        name: str,
    ) -> None:
        if len(tokens) != len(set(tokens)):
            raise StoreProtocolError(
                f"Remote server returned duplicate {name} tokens"
            )

    async def _rollback_staged_registrations(
        self,
        subscriptions: list[
            tuple[RemoteSubscription[Any], str, str]
        ],
        queries: list[
            tuple[RemoteQuerySubscription, str, str]
        ],
        signals: list[
            tuple[RemoteSignalBinding[Any], str, str]
        ],
    ) -> None:
        for _subscription, token, _event_token in subscriptions:
            await self._best_effort_request(
                "unsubscribe",
                {"subscription_token": token},
            )

        for _subscription, token, _event_token in queries:
            await self._best_effort_request(
                "query_unsubscribe",
                {"subscription_token": token},
            )

        for _binding, token, _event_token in signals:
            await self._best_effort_request(
                "signal_unbind",
                {"binding_token": token},
            )

    async def _drop_current_remote_registrations(self) -> None:
        if not self.transport_connected:
            return

        for token in tuple(self._subscriptions_by_token):
            await self._best_effort_request(
                "unsubscribe",
                {"subscription_token": token},
            )

        for token in tuple(self._query_subscriptions_by_token):
            await self._best_effort_request(
                "query_unsubscribe",
                {"subscription_token": token},
            )

        for token in tuple(self._signal_bindings_by_token):
            await self._best_effort_request(
                "signal_unbind",
                {"binding_token": token},
            )

    async def _best_effort_request(
        self,
        event: str,
        message: dict[str, Any],
    ) -> None:
        try:
            await self._raw_request(
                event,
                message,
            )
        except Exception:
            logger.debug(
                "Could not roll back remote registration: %s",
                event,
                exc_info=True,
            )

    # -------------------------------------------------
    # Transactions
    # -------------------------------------------------

    async def transaction(
        self,
        instructions: list[dict[str, Any]],
    ) -> Response[TransactionResult]:
        result = await self._request(
            "transaction",
            {
                "instructions": instructions,
            },
        )

        if result.ok is False:
            return cast(
                Response[TransactionResult],
                result,
            )

        data = result.data

        if not isinstance(data, dict):
            raise StoreProtocolError(
                "Remote transaction response data must be an object"
            )

        try:
            transaction_result = TransactionResult.from_payload(
                data,
            )
        except Exception as exc:
            raise StoreProtocolError(
                "Remote transaction response contains an invalid result"
            ) from exc

        return success(
            transaction_result,
        )

    async def undo(self) -> Response[Any]:
        return await self._request(
            "undo",
        )

    async def redo(self) -> Response[Any]:
        return await self._request(
            "redo",
        )

    # -------------------------------------------------
    # Incoming events
    # -------------------------------------------------

    async def on_events(
        self,
        messages: list[dict[str, Any]],
    ) -> None:
        dead: set[RemoteSubscription[Any]] = set()
        dead_queries: set[RemoteQuerySubscription] = set()

        for message in messages:
            if "query_change" in message:
                dead_queries.update(
                    self._dispatch_query_message(message)
                )
            else:
                dead.update(
                    self._dispatch_event_message(message)
                )

        for subscription in dead:
            try:
                await self.dispose_subscription(subscription)
            except Exception:
                logger.exception(
                    "Could not dispose dead remote subscription"
                )

        for subscription in dead_queries:
            try:
                await self.dispose_query_subscription(subscription)
            except Exception:
                logger.exception(
                    "Could not dispose dead query subscription"
                )

    def _dispatch_event_message(
        self,
        message: dict[str, Any],
    ) -> set[RemoteSubscription[Any]]:
        event_token = message.get("event_token")
        event_payload = message.get("event")

        if not isinstance(event_token, str) or not event_token:
            raise StoreProtocolError(
                "Remote event message has no valid event_token"
            )

        if not isinstance(event_payload, dict):
            raise StoreProtocolError(
                "Remote event message has no event payload"
            )

        subscriptions = tuple(
            self._subscriptions.get(
                event_token,
                (),
            )
        )

        if not subscriptions:
            return set()

        try:
            event: StoreEvent = event_from_payload(
                event_payload,
            )
        except Exception as exc:
            raise StoreProtocolError(
                "Remote event payload is invalid"
            ) from exc

        dead: set[RemoteSubscription[Any]] = set()

        for subscription in subscriptions:
            result = subscription.dispatch(
                event,
            )

            if result is DispatchResult.CALLBACK_GONE:
                dead.add(
                    subscription,
                )
                continue

            if result is DispatchResult.FAILED:
                self._request_address_resync(
                    subscription.address,
                    "subscription-callback-failed",
                )

        return dead

    def _dispatch_query_message(
        self,
        message: dict[str, Any],
    ) -> set[RemoteQuerySubscription]:
        event_token = message.get("event_token")
        payload = message.get("query_change")

        if not isinstance(event_token, str) or not event_token:
            raise StoreProtocolError(
                "Remote query message has no valid event_token"
            )

        subscriptions = tuple(
            self._query_subscriptions.get(event_token, ())
        )

        if not subscriptions:
            return set()

        try:
            change = QueryChangeSet.from_payload(payload)
        except Exception as exc:
            raise StoreProtocolError(
                "Remote query change payload is invalid"
            ) from exc

        dead: set[RemoteQuerySubscription] = set()

        for subscription in subscriptions:
            result = subscription.dispatch(change)

            if result is DispatchResult.CALLBACK_GONE:
                dead.add(subscription)
                continue

            if result is DispatchResult.FAILED:
                self.request_query_resync(
                    subscription,
                    "query-change-failed",
                )

        return dead

    def _request_address_resync(
        self,
        address: Address[Any],
        reason: str,
    ) -> None:
        for adapter in self._loaded_adapters():
            if adapter.address == address:
                self.request_adapter_resync(
                    adapter,
                    reason,
                )

    async def on_signal(
        self,
        message: dict[str, Any],
    ) -> None:
        from kivyfn.store.signals.event import (
            signal_event_from_payload,
        )

        event_token = message.get("event_token")
        signal_payload = message.get("signal")

        if not isinstance(event_token, str) or not event_token:
            raise StoreProtocolError(
                "Remote signal message has no valid event_token"
            )

        if not isinstance(signal_payload, dict):
            raise StoreProtocolError(
                "Remote signal message has no signal payload"
            )

        bindings = tuple(
            self._signal_bindings.get(
                event_token,
                (),
            )
        )

        if not bindings:
            return

        try:
            event = signal_event_from_payload(
                signal_payload,
            )
        except Exception as exc:
            raise StoreProtocolError(
                "Remote signal payload is invalid"
            ) from exc

        dead: list[RemoteSignalBinding[Any]] = []

        for binding in bindings:
            if not await binding.dispatch(event):
                dead.append(
                    binding,
                )

        for binding in dead:
            try:
                await self.dispose_signal_binding(
                    binding,
                )
            except Exception:
                logger.exception(
                    "Could not dispose dead remote signal binding"
                )

    # -------------------------------------------------
    # Helpers
    # -------------------------------------------------

    def _clear_remote_routes(self) -> None:
        self._subscriptions.clear()
        self._subscriptions_by_token.clear()
        self._query_subscriptions.clear()
        self._query_subscriptions_by_token.clear()
        self._signal_bindings.clear()
        self._signal_bindings_by_token.clear()

    @staticmethod
    def _parse_registration(
        data: Any,
        *,
        token_key: str,
        operation: str,
    ) -> tuple[str, str]:
        if not isinstance(data, dict):
            raise StoreProtocolError(
                f"Remote {operation} response data must be an object"
            )

        token = data.get(
            token_key,
        )
        event_token = data.get(
            "event_token",
        )

        if not isinstance(token, str) or not token:
            raise StoreProtocolError(
                f"Remote {operation} response has no valid {token_key}"
            )

        if not isinstance(event_token, str) or not event_token:
            raise StoreProtocolError(
                f"Remote {operation} response has no valid event_token"
            )

        return token, event_token
