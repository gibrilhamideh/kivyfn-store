# src/kivyfn/store/client/client.py
from __future__ import annotations

import asyncio

from typing import Any, Protocol, TypeAlias, cast

from kivyfn.store.client.queue import RequestQueue
from kivyfn.store.client.subscription import (
    ClientSubscription,
    ClientSubscriptions,
    SubscriptionManager,
    SubscriptionSource,
)
from kivyfn.store.error import StoreConnectionError, StoreProtocolError
from kivyfn.store.protocol.message.event import EventMessage, SubscriptionEvent
from kivyfn.store.protocol.message.requests import (
    CollectionSubscriptionRequest,
    QueryRequest,
    QuerySubscriptionRequest,
    RequestAction,
    RequestBatch,
    RequestItem,
    SingleSubscriptionRequest,
    SubscriptionRequest,
    TransactionRequest,
    UnsubscribeRequest,
)
from kivyfn.store.protocol.message.responses import (
    CollectionSubscriptionResponse,
    FailureResponse,
    QueryResponse,
    QuerySubscriptionResponse,
    ResponseBatch,
    ResponseItem,
    SingleSubscriptionResponse,
    SubscriptionResponse,
    TransactionFailureResponse,
    TransactionResponse,
    UnsubscribeResponse,
)
from kivyfn.store.query import Query
from kivyfn.store.transport.endpoint import create_client_transport


TransactionReply: TypeAlias = TransactionResponse | TransactionFailureResponse

QueryReply: TypeAlias = QueryResponse | FailureResponse


class ClientSubscriptionManager(SubscriptionManager, Protocol):
    def on_subscription_snapshot(
        self, source: SubscriptionSource, response: SubscriptionResponse
    ) -> None: ...

    def on_subscription_event(self, event: SubscriptionEvent) -> None: ...

    def on_subscription_status(
        self, source: SubscriptionSource, synced: bool
    ) -> None: ...


class StoreClient:
    """Coordinate client batching, subscriptions, and transport events."""

    def __init__(
        self, endpoint: str, *, max_buffered_messages: int = 1000
    ) -> None:
        self._endpoint = endpoint
        self._transport = create_client_transport(endpoint, self)

        self._max_buffered_messages = max_buffered_messages

        self._online = False
        self._resubscribing = False

        self._subscriptions = ClientSubscriptions()
        self._queue = RequestQueue()

        self._loop: asyncio.AbstractEventLoop | None = None

        self._batch_handle: asyncio.Handle | None = None
        self._flush_task: asyncio.Task[None] | None = None

        self._sync_batch: RequestBatch | None = None
        self._sync_future: asyncio.Future[None] | None = None

        self._messages: list[EventMessage] = []

        self._store_instance_id: str | None = None
        self._connection_instance_id: str | None = None

        self._subscription_requests: dict[int, SubscriptionSource] = {}

        self._transactions: dict[str, asyncio.Future[TransactionReply]] = {}

        self._queries: dict[int, asyncio.Future[QueryReply]] = {}

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def online(self) -> bool:
        return self._online

    @property
    def synced(self) -> bool:
        return (
            self._online
            and not self._resubscribing
            and self._subscriptions.synced
        )

    async def connect(self) -> None:
        self._loop = asyncio.get_running_loop()

        if self.online:
            return

        self._sync_future = self._loop.create_future()

        await self._transport.connect()

        sync = self._sync_future

        if sync is not None:
            await asyncio.shield(sync)

    async def disconnect(self) -> None:
        flush = self._flush_task

        if self._batch_handle is not None:
            self._batch_handle.cancel()
            self._batch_handle = None

        if flush is not None:
            flush.cancel()

        await self._transport.disconnect()

        if flush is not None:
            await asyncio.gather(flush, return_exceptions=True)

        self._online = False
        self._resubscribing = False
        self._sync_batch = None
        self._flush_task = None

        self._messages.clear()
        self._queue.clear()

        self._subscriptions.disconnect()

        self._set_subscription_status(False)

        error = StoreConnectionError("Store client disconnected")

        for future in self._transactions.values():
            if not future.done():
                future.set_exception(error)

        for future in self._queries.values():
            if not future.done():
                future.set_exception(error)

        self._transactions.clear()
        self._queries.clear()
        self._subscription_requests.clear()

        sync = self._sync_future

        if sync is not None and not sync.done():
            sync.set_exception(error)

        self._sync_future = None
        self._loop = None

    def on_connect(self) -> None:
        """Handle a transport connection event."""
        self._online = True
        self._resubscribing = True
        self._connection_instance_id = None

        if self._loop is None:
            self._loop = asyncio.get_running_loop()

        if self._sync_future is None or self._sync_future.done():
            self._sync_future = self._loop.create_future()

        subscriptions = self._subscriptions.desired()

        if not subscriptions:
            self._finish_resubscribe()
            return

        requests: list[RequestItem] = []

        for subscription in subscriptions:
            request = subscription.request(self._store_instance_id)

            self._subscription_requests[id(request)] = subscription.source

            requests.append(request)

        batch = RequestBatch(items=tuple(requests))

        self._sync_batch = batch

        self._queue.appendleft(batch)

        self._schedule_flush()

    def on_disconnect(self) -> None:
        """Handle an unexpected transport disconnect event."""
        if not self._online:
            return

        self._online = False
        self._resubscribing = False
        self._connection_instance_id = None
        self._sync_batch = None

        self._messages.clear()

        self._subscriptions.disconnect()

        self._set_subscription_status(False)

    def on_message(self, message: EventMessage) -> None:
        """Handle one EventMessage received by the transport."""
        if not self._online:
            return

        if self._resubscribing:
            if len(self._messages) >= self._max_buffered_messages:
                raise StoreProtocolError("Store message buffer is full")

            self._messages.append(message)

            return

        self._deliver(message)

    def subscribe(
        self, source: SubscriptionSource, manager: ClientSubscriptionManager
    ) -> ClientSubscription:
        existing = self._subscriptions.get(source)

        subscription = self._subscriptions.get_or_create(source, manager)

        if existing is not None:
            return subscription

        manager.on_subscription_status(source, False)

        if not self.online:
            return subscription

        request = subscription.request(self._store_instance_id)

        self._subscription_requests[id(request)] = source

        if self._resubscribing:
            self._queue.appendleft(RequestBatch(items=(request,)))

            self._schedule_flush()

            return subscription

        self._enqueue(request)

        return subscription

    def unsubscribe(
        self, source: SubscriptionSource, manager: ClientSubscriptionManager
    ) -> None:
        subscription = self._subscriptions.get(source)

        if subscription is None:
            return

        if subscription.manager is not manager:
            raise RuntimeError("Subscription belongs to another StateManager")

        subscription_id = subscription.subscription_id

        self._subscriptions.remove(source)

        if self.online and subscription_id is not None:
            self._enqueue(UnsubscribeRequest(subscription_id=subscription_id))

    def transaction(
        self, *, transaction_id: str, actions: tuple[RequestAction, ...]
    ) -> asyncio.Future[TransactionReply]:
        if transaction_id in self._transactions:
            raise RuntimeError(
                f"Transaction already exists: {transaction_id!r}"
            )

        future: asyncio.Future[TransactionReply] = self._future()

        self._transactions[transaction_id] = future

        self._enqueue(
            TransactionRequest(transaction_id=transaction_id, actions=actions)
        )

        return future

    def query(self, query: Query[Any]) -> asyncio.Future[QueryReply]:
        request = QueryRequest(query=query)

        future: asyncio.Future[QueryReply] = self._future()

        self._queries[id(request)] = future

        self._enqueue(request)

        return future

    def _enqueue(self, request: RequestItem) -> None:
        self._queue.append(request)

        self._schedule_batch()

    def _schedule_batch(self) -> None:
        if self._loop is None:
            raise RuntimeError("StoreClient is not running")

        if self._batch_handle is not None:
            return

        self._batch_handle = self._loop.call_soon(self._batch)

    def _batch(self) -> None:
        self._batch_handle = None

        self._queue.batch()

        self._schedule_flush()

    def _schedule_flush(self) -> None:
        if not self._online:
            return

        if not self._queue.has_batches:
            return

        if self._loop is None:
            return

        if self._flush_task is not None:
            return

        self._flush_task = self._loop.create_task(self._flush())

    async def _flush(self) -> None:
        try:
            while self._online:
                batch = self._queue.popleft()

                if batch is None:
                    return

                if self._resubscribing and batch is not self._sync_batch:
                    self._queue.appendleft(batch)

                    return

                try:
                    response = await self._transport.request(batch)

                except asyncio.CancelledError:
                    self._queue.appendleft(batch)

                    raise

                except Exception:
                    self._queue.appendleft(batch)

                    return

                try:
                    self._handle_response_batch(batch, response)

                except Exception as error:
                    self._fail_batch(batch, error)

                    return

                if batch is self._sync_batch:
                    self._sync_batch = None

                    if not self._subscriptions.synced:
                        sync = self._sync_future

                        if sync is not None and not sync.done():
                            sync.set_exception(
                                StoreProtocolError(
                                    "Subscription synchronization failed"
                                )
                            )

                        return

                    self._finish_resubscribe()

        finally:
            if self._flush_task is asyncio.current_task():
                self._flush_task = None

            self._schedule_flush()

    def _handle_response_batch(
        self, request: RequestBatch, response: ResponseBatch
    ) -> None:
        if len(request.items) != len(response.items):
            raise StoreProtocolError(
                "Response batch does not match request batch"
            )

        for request_item, response_item in zip(request.items, response.items):
            self._handle_response(request_item, response_item)

    def _handle_response(
        self, request: RequestItem, response: ResponseItem
    ) -> None:
        if isinstance(request, TransactionRequest):
            self._handle_transaction_response(request, response)

            return

        if isinstance(request, QueryRequest):
            self._handle_query_response(request, response)

            return

        if isinstance(
            request,
            (
                SingleSubscriptionRequest,
                CollectionSubscriptionRequest,
                QuerySubscriptionRequest,
            ),
        ):
            self._handle_subscription_response(request, response)

            return

        if isinstance(request, UnsubscribeRequest):
            self._handle_unsubscribe_response(request, response)

    def _handle_transaction_response(
        self, request: TransactionRequest, response: ResponseItem
    ) -> None:
        if not isinstance(
            response, (TransactionResponse, TransactionFailureResponse)
        ):
            raise StoreProtocolError("Invalid Transaction response")

        if response.transaction_id != request.transaction_id:
            raise StoreProtocolError("Transaction response ID does not match")

        future = self._transactions.pop(request.transaction_id, None)

        if future is not None and not future.done():
            future.set_result(response)

    def _handle_query_response(
        self, request: QueryRequest, response: ResponseItem
    ) -> None:
        if not isinstance(response, (QueryResponse, FailureResponse)):
            raise StoreProtocolError("Invalid Query response")

        future = self._queries.pop(id(request), None)

        if future is not None and not future.done():
            future.set_result(response)

    def _handle_subscription_response(
        self, request: SubscriptionRequest, response: ResponseItem
    ) -> None:
        source = self._subscription_requests.pop(id(request), None)

        if source is None:
            raise StoreProtocolError("Unknown subscription request")

        if isinstance(response, FailureResponse):
            return

        if not isinstance(
            response,
            (
                SingleSubscriptionResponse,
                CollectionSubscriptionResponse,
                QuerySubscriptionResponse,
            ),
        ):
            raise StoreProtocolError("Invalid Subscription response")

        self._check_connection_instance(response.store_instance_id)

        subscription = self._subscriptions.get(source)

        if subscription is None:
            self._enqueue(
                UnsubscribeRequest(subscription_id=response.subscription_id)
            )

            return

        if subscription.active:
            if response.subscription_id != subscription.subscription_id:
                self._enqueue(
                    UnsubscribeRequest(
                        subscription_id=response.subscription_id
                    )
                )

            return

        revisions = subscription.response_revisions(
            response, store_instance_id=request.store_instance_id
        )

        manager = _subscription_manager(subscription)

        manager.on_subscription_snapshot(subscription.source, response)

        self._subscriptions.bind(subscription, response, revisions)

        self._store_instance_id = response.store_instance_id

        if not self._resubscribing:
            manager.on_subscription_status(subscription.source, True)

    def _handle_unsubscribe_response(
        self, request: UnsubscribeRequest, response: ResponseItem
    ) -> None:
        if isinstance(response, FailureResponse):
            return

        if not isinstance(response, UnsubscribeResponse):
            raise StoreProtocolError("Invalid Unsubscribe response")

        if response.subscription_id != request.subscription_id:
            raise StoreProtocolError("Unsubscribe response ID does not match")

    def _finish_resubscribe(self) -> None:
        messages = tuple(self._messages)

        self._messages.clear()

        for message in messages:
            self._deliver(message)

        self._resubscribing = False

        self._set_subscription_status(True)

        sync = self._sync_future

        if sync is not None and not sync.done():
            sync.set_result(None)

        self._schedule_flush()

    def _deliver(self, message: EventMessage) -> None:
        self._check_connection_instance(message.store_instance_id)

        for event in message.events:
            subscription = self._subscriptions.get_by_id(event.subscription_id)

            if subscription is None:
                continue

            revisions = subscription.next_revisions(event)

            manager = _subscription_manager(subscription)

            manager.on_subscription_event(event)

            subscription.advance(revisions)

    def _check_connection_instance(self, store_instance_id: str) -> None:
        current = self._connection_instance_id

        if current is not None and current != store_instance_id:
            raise StoreProtocolError(
                "Store instance changed on one connection"
            )

        self._connection_instance_id = store_instance_id

    def _set_subscription_status(self, synced: bool) -> None:
        for subscription in self._subscriptions.desired():
            manager = _subscription_manager(subscription)

            manager.on_subscription_status(subscription.source, synced)

    def _fail_batch(self, batch: RequestBatch, error: Exception) -> None:
        for request in batch.items:
            if isinstance(request, TransactionRequest):
                future = self._transactions.pop(request.transaction_id, None)

                if future is not None and not future.done():
                    future.set_exception(error)

                continue

            if isinstance(request, QueryRequest):
                future = self._queries.pop(id(request), None)

                if future is not None and not future.done():
                    future.set_exception(error)

    def _future(self) -> asyncio.Future[Any]:
        if self._loop is None:
            raise RuntimeError("StoreClient is not running")

        return self._loop.create_future()


def _subscription_manager(
    subscription: ClientSubscription,
) -> ClientSubscriptionManager:
    return cast(ClientSubscriptionManager, subscription.manager)
