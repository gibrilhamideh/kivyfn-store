# src/kivyfn/store/client/client.py
from __future__ import annotations

import asyncio
from asyncio import sleep
from collections import defaultdict
from collections.abc import Callable
from copy import deepcopy
from math import isfinite
from typing import Any, cast
from uuid import uuid4

from kivyfn.logging import get_logger

from kivyfn.store.client.future import (
    HistoryReply,
    QueryReply,
    RequestFutures,
    TransactionReply,
)
from kivyfn.store.client.queue import RequestQueue
from kivyfn.store.client.subscription import (
    ClientSubscription,
    ClientSubscriptions,
    SubscriptionManager,
    SubscriptionSource,
    SubscriptionUpdate,
    publish_subscriptions,
)
from kivyfn.store.client.validation import (
    ResponsePairs,
    validate_response_batch,
)
from kivyfn.store.error import (
    StoreConnectionError,
    StoreProtocolError,
    StoreStateError,
    StoreSubscriptionError,
)
from kivyfn.store.protocol.message.event import EventMessage
from kivyfn.store.protocol.message.requests import (
    CollectionSubscriptionRequest,
    HistoryRequest,
    QueryRequest,
    QuerySubscriptionRequest,
    RedoRequest,
    RequestAction,
    RequestBatch,
    RequestItem,
    SingleSubscriptionRequest,
    SubscriptionRequest,
    TransactionRequest,
    UndoRequest,
    UnsubscribeRequest,
)
from kivyfn.store.protocol.message.responses import (
    FailureResponse,
    ResponseBatch,
    ResponseItem,
    SubscriptionResponse,
    TransactionFailureResponse,
)
from kivyfn.store.query import Query
from kivyfn.store.store.history import HistoryPosition
from kivyfn.store.transport.client import ClientTransport
from kivyfn.store.transport.endpoint import create_client_transport

logger = get_logger(__name__)


class StoreClient:
    """Batch requests and own subscription synchronization.

    An interrupted request is never automatically replayed. Its outcome may
    be unknown, so reconnect obtains current canonical snapshots. A positive
    reconnect_interval enables recovery after an established connection is
    lost. Initial connect failures stay visible, and disconnect stops retries.
    """

    def __init__(
        self,
        *,
        max_buffered_messages: int = 1000,
        reconnect_interval: float | None = None,
    ) -> None:
        if max_buffered_messages < 1:
            raise ValueError("max_buffered_messages must be positive")
        if reconnect_interval is not None and (
            isinstance(reconnect_interval, bool)
            or not isinstance(reconnect_interval, (int, float))
            or not isfinite(reconnect_interval)
            or reconnect_interval <= 0
        ):
            raise ValueError("reconnect_interval must be positive and finite")
        self._reconnect_interval = reconnect_interval
        self._reconnect_enabled = False
        self._reconnect_task: asyncio.Task[None] | None = None
        self._disconnect_task: asyncio.Task[None] | None = None
        self._endpoint: str | None = None
        self._transport: ClientTransport | None = None
        self._max_buffered_messages = max_buffered_messages
        self._online = False
        self._generation = 0
        self._resubscribing = False
        self._subscriptions = ClientSubscriptions()
        self._queue = RequestQueue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._batch_handle: asyncio.Handle | None = None
        self._flush_task: asyncio.Task[None] | None = None
        self._connect_task: asyncio.Task[None] | None = None
        self._sync_future: asyncio.Future[None] | None = None
        self._messages: list[EventMessage] = []
        self._store_instance_id: str | None = None
        self._connection_instance_id: str | None = None
        self._subscription_requests: dict[int, ClientSubscription] = {}
        self._futures = RequestFutures()
        self._subscription_handlers: dict[type, Callable[..., None]] = {
            SingleSubscriptionRequest: self._handle_subscription_response,
            CollectionSubscriptionRequest: self._handle_subscription_response,
            QuerySubscriptionRequest: self._handle_subscription_response,
            UnsubscribeRequest: self._handle_unsubscribe_response,
        }

    @property
    def endpoint(self) -> str | None:
        return self._endpoint

    @property
    def online(self) -> bool:
        return self._online

    @property
    def synced(self) -> bool:
        return (
            self.online
            and not self._resubscribing
            and not self._subscription_requests
            and self._subscriptions.synced
        )

    async def connect(self, endpoint: str | None = None) -> None:
        """Connect and sync, retrying failed subscriptions when already online.

        A StoreSubscriptionError leaves the connection and successfully bound
        subscriptions usable. A later connect() retries only inactive sources.
        """
        loop = asyncio.get_running_loop()
        self._require_loop()
        disconnecting = self._disconnect_task
        if disconnecting is not None:
            await asyncio.shield(disconnecting)
        if endpoint is None:
            endpoint = self._endpoint
        if endpoint is None:
            raise StoreConnectionError("An endpoint is required to connect")
        if endpoint != self._endpoint:
            if self._loop is not None:
                raise StoreConnectionError(
                    "Disconnect before changing the Store endpoint"
                )
            transport = create_client_transport(endpoint, self)
            self._transport = transport
            self._endpoint = endpoint
        self._loop = loop
        if self._connect_task is None:
            if self.online:
                for subscription in self._subscriptions.inactive():
                    self._subscribe(subscription)
                if self._sync_future is not None:
                    await asyncio.shield(self._sync_future)
                return
            self._connect_task = loop.create_task(self._connect())
            self._connect_task.add_done_callback(self._connect_done)
        await asyncio.shield(self._connect_task)

    def _connect_done(self, task: asyncio.Task[None]) -> None:
        # A cancelled waiter does not own the shared connection attempt.
        if self._connect_task is task:
            self._connect_task = None
        if not task.cancelled():
            task.exception()

    async def _connect(self) -> None:
        transport = self._require_transport()
        assert self._loop is not None
        sync = self._loop.create_future()
        sync.add_done_callback(_observe_completion)
        self._sync_future = sync
        try:
            await transport.connect()
            await asyncio.shield(sync)
            if not self.online:
                raise StoreConnectionError(
                    "Store connection lost during synchronization"
                )
            self._reconnect_enabled = True
        except StoreSubscriptionError:
            # A valid rejection does not invalidate the transport or peers.
            self._reconnect_enabled = self.online
            raise
        except BaseException as error:
            if isinstance(error, StoreProtocolError):
                self._reconnect_enabled = False
            self._reset(StoreConnectionError("Store connection failed"))
            if sync.done() and not sync.cancelled():
                sync.exception()
            if self._disconnect_task is None or self._disconnect_task.done():
                await transport.disconnect()
            raise

    async def disconnect(self) -> None:
        """Stop recovery and finish one shared connection teardown."""
        self._require_loop()
        self._reconnect_enabled = False
        reconnecting = self._reconnect_task
        if reconnecting is not None:
            reconnecting.cancel()
        error = StoreConnectionError(
            "Store client disconnected; in-flight outcomes may be unknown"
        )
        await asyncio.shield(
            self._begin_disconnect(error, cancel_connect=True)
        )
        if reconnecting is not None:
            await asyncio.gather(reconnecting, return_exceptions=True)

    def _begin_disconnect(
        self, error: Exception, *, cancel_connect: bool = False
    ) -> asyncio.Task[None]:
        task = self._disconnect_task
        if task is None or task.done():
            self._reset(error)
            task = asyncio.create_task(self._disconnect(cancel_connect))
            self._disconnect_task = task
            task.add_done_callback(_observe_completion)
        return task

    async def _disconnect(self, cancel_connect: bool) -> None:
        tasks = tuple(
            task
            for task in (self._flush_task, self._connect_task)
            if task is not None
        )
        for task in tasks:
            if task is not self._connect_task or cancel_connect:
                task.cancel()
        transport = self._transport
        try:
            if transport is not None:
                await transport.disconnect()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._flush_task = None
            self._connect_task = None
            self._sync_future = None
            self._loop = None

    def _require_loop(self) -> None:
        if self._loop is not None and self._loop is not (
            asyncio.get_running_loop()
        ):
            raise RuntimeError("StoreClient belongs to another event loop")

    def on_connect(self) -> None:
        self._generation += 1
        self._online = True
        self._resubscribing = True
        self._connection_instance_id = None
        for subscription in self._subscriptions.desired():
            self._subscribe(subscription)
        self._finish_sync()

    def on_disconnect(self, error: Exception | None = None) -> None:
        if error is None:
            error = StoreConnectionError(
                "Store connection lost; in-flight outcomes may be unknown"
            )
        self._begin_disconnect(error)
        if isinstance(error, StoreProtocolError):
            self._reconnect_enabled = False
        if (
            self._reconnect_enabled
            and self._reconnect_interval is not None
            and (self._reconnect_task is None or self._reconnect_task.done())
        ):
            task = asyncio.create_task(self._reconnect())
            self._reconnect_task = task
            task.add_done_callback(self._reconnect_done)

    async def _reconnect(self) -> None:
        assert self._reconnect_interval is not None
        while self._reconnect_enabled:
            disconnecting = self._disconnect_task
            if disconnecting is not None:
                await asyncio.shield(disconnecting)
            await sleep(self._reconnect_interval)
            if self.online:
                return
            try:
                await self.connect()
                return
            except StoreSubscriptionError:
                # Keep healthy subscriptions; retry rejected sources manually.
                logger.exception(
                    "Store reconnected with rejected subscriptions"
                )
                return
            except StoreProtocolError:
                self._reconnect_enabled = False
                logger.exception(
                    "Store reconnect stopped: incompatible protocol"
                )
                return
            except Exception:
                logger.warning(
                    "Store reconnect failed; will retry", exc_info=True
                )

    def _reconnect_done(self, task: asyncio.Task[None]) -> None:
        if self._reconnect_task is task:
            self._reconnect_task = None
        if not task.cancelled():
            task.exception()

    def _reset(self, error: Exception) -> None:
        self._generation += 1
        self._online = False
        self._resubscribing = False
        self._connection_instance_id = None
        if self._batch_handle is not None:
            self._batch_handle.cancel()
            self._batch_handle = None
        self._queue.clear()
        self._messages.clear()
        self._subscription_requests.clear()
        self._subscriptions.disconnect()
        self._set_subscription_status(False)
        self._futures.fail_all(error)
        sync = self._sync_future
        if sync is not None and not sync.done():
            sync.set_exception(error)

    def on_message(self, message: EventMessage) -> None:
        if not self.online:
            return
        if self._resubscribing or self._subscription_requests:
            if len(self._messages) >= self._max_buffered_messages:
                raise StoreProtocolError("Store message buffer is full")
            self._messages.append(message)
        else:
            self._deliver(message)

    def subscribe(
        self, source: SubscriptionSource, manager: SubscriptionManager
    ) -> ClientSubscription:
        existing = self._subscriptions.get(source)
        subscription = self._subscriptions.get_or_create(source, manager)
        if existing is None:
            manager.on_subscription_status(source, False)
            if self.online:
                self._subscribe(subscription)
        return subscription

    def _subscribe(self, subscription: ClientSubscription) -> None:
        if any(
            pending is subscription
            for pending in self._subscription_requests.values()
        ):
            return
        if self._sync_future is None or self._sync_future.done():
            assert self._loop is not None
            self._sync_future = self._loop.create_future()
            self._sync_future.add_done_callback(_observe_completion)
        subscription.failure = None
        request = subscription.request(self._store_instance_id)
        self._subscription_requests[id(request)] = subscription
        self._enqueue(request)

    def unsubscribe(
        self, source: SubscriptionSource, manager: SubscriptionManager
    ) -> None:
        subscription = self._subscriptions.get(source)
        if subscription is None:
            return
        if subscription.manager is not manager:
            raise RuntimeError("Subscription belongs to another StateManager")
        subscription_id = subscription.subscription_id
        self._subscriptions.remove(source)
        manager.on_subscription_status(source, False)
        if self.online and subscription_id is not None:
            self._enqueue(UnsubscribeRequest(subscription_id))
        self._finish_sync()

    def transaction(
        self,
        *,
        transaction_id: str,
        actions: tuple[RequestAction, ...],
        history_group: str | None = None,
        history_label: str | None = None,
        expected_store_instance_id: str | None = None,
        expected_revisions: dict[str, int] | None = None,
    ) -> asyncio.Future[TransactionReply]:
        self._require_online()
        request = TransactionRequest(
            transaction_id,
            deepcopy(actions),
            history_group,
            history_label,
            expected_store_instance_id,
            deepcopy(expected_revisions),
        )
        future = self._futures.create_transaction(request)
        self._enqueue(request)
        return future

    def history(
        self, *, limit: int = 100, offset: int = 0
    ) -> asyncio.Future[HistoryReply]:
        self._require_online()
        request = HistoryRequest(limit, offset)
        future = self._futures.create_history(request)
        self._enqueue(request)
        return future

    async def undo(
        self,
        *,
        expected_position: HistoryPosition | None = None,
        transaction_id: str | None = None,
    ) -> TransactionReply:
        return await self._navigate(
            UndoRequest, expected_position, transaction_id
        )

    async def redo(
        self,
        *,
        expected_position: HistoryPosition | None = None,
        transaction_id: str | None = None,
    ) -> TransactionReply:
        return await self._navigate(
            RedoRequest, expected_position, transaction_id
        )

    async def _navigate(
        self,
        request_type: type[UndoRequest] | type[RedoRequest],
        expected_position: HistoryPosition | None,
        transaction_id: str | None,
    ) -> TransactionReply:
        self._require_navigation_ready()
        command_id = uuid4().hex if transaction_id is None else transaction_id
        if expected_position is None:
            history = await self.history(limit=1)
            if isinstance(history, FailureResponse):
                return TransactionFailureResponse(command_id, history.failure)
            status = history.page.status
            expected_position = (
                status.undo_position
                if request_type is UndoRequest
                else status.redo_position
            )
            # A mutation may have been submitted while status was in flight.
            self._require_navigation_ready()
        request = request_type(command_id, expected_position)
        future = self._futures.create_transaction(request)
        self._enqueue(request)
        return await future

    def _require_navigation_ready(self) -> None:
        self._require_online()
        # A cancelled waiter still has unresolved queued or in-flight work.
        if self._futures.has_pending_transactions:
            raise StoreStateError(
                "Await pending local mutations before undo or redo"
            )

    def query(self, query: Query[Any]) -> asyncio.Future[QueryReply]:
        self._require_online()
        request = QueryRequest(query)
        future = self._futures.create_query(request)
        self._enqueue(request)
        return future

    def _enqueue(self, request: RequestItem) -> None:
        self._queue.append(request)
        if self._batch_handle is None:
            assert self._loop is not None
            self._batch_handle = self._loop.call_soon(self._batch)

    def _batch(self) -> None:
        self._batch_handle = None
        self._queue.batch()
        self._schedule_flush()

    def _schedule_flush(self) -> None:
        if (
            self.online
            and self._queue.has_batches
            and self._flush_task is None
        ):
            assert self._loop is not None
            self._flush_task = self._loop.create_task(self._flush())

    async def _flush(self) -> None:
        transport = self._require_transport()
        try:
            while self.online:
                batch = self._queue.popleft()
                if batch is None:
                    return
                generation = self._generation
                try:
                    response = await transport.request(batch)
                    if not self.online or generation != self._generation:
                        return
                    self._handle_response_batch(batch, response)
                    self._finish_sync()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    if generation != self._generation:
                        return
                    logger.exception(
                        "Store client batch failed; disconnecting"
                    )
                    # Sent work may already have committed. Never replay it.
                    self.on_disconnect(error)
                    return
        finally:
            if self._flush_task is asyncio.current_task():
                self._flush_task = None
            self._schedule_flush()

    def _handle_response_batch(
        self, request: RequestBatch, response: ResponseBatch
    ) -> None:
        """Install subscription replies and buffered events before completion.

        Validate the whole batch first so a malformed later reply cannot
        complete an earlier request. Snapshots must precede buffered events.
        """
        pairs = validate_response_batch(request, response)
        self._apply_subscription_responses(pairs)
        self._drain_messages()
        for item, reply in pairs:
            self._futures.resolve(item, reply)

    def _apply_subscription_responses(self, pairs: ResponsePairs) -> None:
        for item, reply in pairs:
            handler = self._subscription_handlers.get(type(item))
            if handler is not None:
                handler(item, reply)

    def _handle_unsubscribe_response(
        self, request: UnsubscribeRequest, response: ResponseItem
    ) -> None:
        if isinstance(response, FailureResponse):
            logger.warning(
                "Store unsubscribe %s failed: %s",
                request.subscription_id,
                response.failure.message,
            )

    def _handle_subscription_response(
        self, request: SubscriptionRequest, response: ResponseItem
    ) -> None:
        requested = self._subscription_requests.pop(id(request), None)
        if requested is None:
            raise StoreProtocolError("Unknown subscription request")
        source = requested.source
        subscription = self._subscriptions.get(source)
        if isinstance(response, FailureResponse):
            # Ignore failures belonging to a released or replaced subscription.
            if subscription is requested and not requested.active:
                requested.failure = response.failure
                requested.manager.on_subscription_status(source, False)
            return
        reply = cast(SubscriptionResponse, response)
        self._check_connection_instance(reply.store_instance_id)
        if subscription is None:
            self._enqueue(UnsubscribeRequest(reply.subscription_id))
            return
        if subscription.active:
            if reply.subscription_id != subscription.subscription_id:
                self._enqueue(UnsubscribeRequest(reply.subscription_id))
            return
        revisions = subscription.response_revisions(
            reply, store_instance_id=request.store_instance_id
        )
        manager = subscription.manager
        self._subscriptions.bind(subscription, reply, revisions)
        self._store_instance_id = reply.store_instance_id
        manager.on_subscription_snapshot(source, reply)

    def _drain_messages(self) -> None:
        messages, self._messages = self._messages, []
        for message in messages:
            self._deliver(message)

    def _finish_sync(self) -> None:
        if not self.online or self._subscription_requests:
            return
        self._drain_messages()
        if not self.online or self._subscription_requests:
            return
        self._resubscribing = False
        self._set_subscription_status(True)
        # State callbacks may release or create subscriptions here.
        if not self.online or self._subscription_requests:
            return
        sync = self._sync_future
        for subscription in self._subscriptions.desired():
            if subscription.failure is not None:
                if sync is not None and not sync.done():
                    sync.set_exception(
                        StoreSubscriptionError(subscription.failure)
                    )
                return
        if not self._subscriptions.synced:
            return
        if sync is not None and sync.done() and sync.exception() is not None:
            # Removing the failed desired source resolves synchronization.
            sync = self._future()
            sync.add_done_callback(_observe_completion)
            self._sync_future = sync
        if sync is not None and not sync.done():
            sync.set_result(None)

    def _deliver(self, message: EventMessage) -> None:
        """Resolve server IDs, stage all managers, then publish one commit.

        Check every subscription revision before staging any manager. Advance
        revisions after staging, before bindings can submit follow-up work.
        """
        self._check_connection_instance(message.store_instance_id)
        prepared: list[tuple[ClientSubscription, dict[str, int]]] = []
        grouped: dict[int, list[SubscriptionUpdate]] = defaultdict(list)
        managers: dict[int, SubscriptionManager] = {}
        seen: set[str] = set()
        for event in message.events:
            if event.subscription_id in seen:
                raise StoreProtocolError("Duplicate subscription in message")
            seen.add(event.subscription_id)
            subscription = self._subscriptions.get_by_id(event.subscription_id)
            if subscription is None:
                continue
            revisions = subscription.next_revisions(event)
            prepared.append((subscription, revisions))
            manager = subscription.manager
            managers[id(manager)] = manager
            grouped[id(manager)].append(
                SubscriptionUpdate(subscription.source, event.actions)
            )
        # Validate the entire envelope before changing any manager's state.
        publish = []
        for identity, updates in grouped.items():
            publish.append(
                managers[identity].prepare_subscription_updates(
                    tuple(updates), transaction_id=message.transaction_id
                )
            )
        for subscription, revisions in prepared:
            subscription.advance(revisions)
        publish_subscriptions(publish)

    def _check_connection_instance(self, store_instance_id: str) -> None:
        current = self._connection_instance_id
        if current is not None and current != store_instance_id:
            raise StoreProtocolError("Store instance changed on connection")
        self._connection_instance_id = store_instance_id

    def _set_subscription_status(self, synced: bool) -> None:
        for subscription in self._subscriptions.desired():
            subscription.manager.on_subscription_status(
                subscription.source, synced and subscription.active
            )

    def _require_online(self) -> None:
        if not self.online:
            raise StoreConnectionError("Store client is disconnected")

    def _require_transport(self) -> ClientTransport:
        transport = self._transport
        if transport is None:
            raise StoreConnectionError("Store client has no transport")
        return transport

    def _future(self) -> asyncio.Future[Any]:
        assert self._loop is not None
        return self._loop.create_future()


def _observe_completion(future: asyncio.Future[None]) -> None:
    # Incremental subscriptions may have no connect() waiter. Preserve the
    # exception for future awaiters without an unobserved-Future warning.
    if not future.cancelled():
        future.exception()
