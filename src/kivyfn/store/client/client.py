# src/kivyfn/store/client/client.py
from __future__ import annotations

import asyncio
import inspect
import math

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any, Protocol, TypeAlias, cast
from uuid import uuid4

from kivyfn.store.client.subscription import (
    ClientSubscription,
    ClientSubscriptions,
    SubscriptionSource,
)
from kivyfn.store.error import (
    StoreConnectionError,
    StoreStateError,
    StoreTimeoutError,
)
from kivyfn.store.events import InsertEvent, RemoveEvent, SetEvent, SetRowEvent
from kivyfn.store.protocol.message.event import EventMessage
from kivyfn.store.protocol.message.requests import (
    CollectionSubscriptionRequest,
    InsertRequest,
    MutationRequest,
    OperationRequest,
    QueryRequest,
    QuerySubscriptionRequest,
    RemoveRequest,
    Request,
    RequestType,
    SetRequest,
    SetRowRequest,
    SignalRequest,
    SignalRowRequest,
    SingleSubscriptionRequest,
    TransactionRequest,
    UnsubscribeRequest,
)
from kivyfn.store.protocol.message.responses import (
    CollectionSubscriptionResponse,
    FailureResponse,
    InsertResponse,
    QueryResponse,
    QuerySubscriptionResponse,
    RemoveResponse,
    Response,
    ResponseItem,
    SetResponse,
    SetRowResponse,
    SignalResponse,
    SingleSubscriptionResponse,
    TransactionResponse,
    UnsubscribeResponse,
)
from kivyfn.store.query import Query, QueryResult, QueryRow
from kivyfn.store.schema import SingleSchema
from kivyfn.store.transport.endpoint import create_client_transport


SubscriptionResponse: TypeAlias = (
    SingleSubscriptionResponse
    | CollectionSubscriptionResponse
    | QuerySubscriptionResponse
)

_WriteResponse: TypeAlias = (
    SetResponse
    | SetRowResponse
    | InsertResponse
    | RemoveResponse
    | TransactionResponse
)

_WRITE_RESPONSES = (
    SetResponse,
    SetRowResponse,
    InsertResponse,
    RemoveResponse,
    TransactionResponse,
)


class SubscriptionConsumer(Protocol):
    """Synchronous interface implemented by StateManager or another owner.

    apply_snapshot() and apply_event() stage confirmed data without publishing
    notifications or reentering the client. publish() runs after every affected
    consumer has staged its data and the revision counters have advanced.
    Event messages are filtered to the supplied subscriptions. Query-only
    dependency metadata never represents a whole-schema snapshot.

    has_cache() must return True only while the complete confirmed view is
    retained, independently of optimistic values. on_status() observes ready
    and connection changes; error describes recovery or failure when present.
    """

    def has_cache(self, subscription: ClientSubscription) -> bool: ...

    def apply_snapshot(
        self, subscription: ClientSubscription, response: SubscriptionResponse
    ) -> None: ...

    def apply_event(
        self,
        message: EventMessage,
        subscriptions: tuple[ClientSubscription, ...],
    ) -> None: ...

    def publish(
        self, subscriptions: tuple[ClientSubscription, ...]
    ) -> None: ...

    def on_status(
        self, subscription: ClientSubscription, error: Exception | None
    ) -> None: ...


@dataclass(slots=True)
class _Pending:
    future: asyncio.Future[ResponseItem]
    subscriptions: tuple[ClientSubscription, ...]
    timer: asyncio.TimerHandle
    response: ResponseItem | None = None


class StoreClient:
    """One event-loop owner for requests, subscriptions, and recovery.

    subscribe() records local intent, including before connect(). connect()
    and synchronize() wait for subscription initialization. Unexpected loss
    reconnects automatically; disconnect() stops retrying but keeps intent.

    Write futures wait for the response and relevant confirmed data in the
    subscriptions present when submitted.
    They never apply response data or resend interrupted writes. A timeout
    or disconnect may leave a write's Store outcome unknown. StateManager
    owns optimistic layers and operation notifications.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        reconnect_delay: float = 1.0,
        request_timeout: float = 10.0,
        max_buffered_events: int = 1000,
    ) -> None:
        for value in (reconnect_delay, request_timeout):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Client timeouts must be finite and positive")
        if type(max_buffered_events) is not int or max_buffered_events < 1:
            raise ValueError("Event buffer size must be a positive integer")
        self._endpoint = endpoint
        self._transport = create_client_transport(endpoint)
        self._reconnect_delay = reconnect_delay
        self._request_timeout = request_timeout
        self._max_buffered_events = max_buffered_events
        self._subscriptions = ClientSubscriptions()
        self._released: set[ClientSubscription] = set()
        self._orphans: set[str] = set()
        self._queued: list[RequestType] = []
        self._pending: dict[str, _Pending] = {}
        self._buffer: list[EventMessage] = []
        self._flush_task: asyncio.Task[None] | None = None
        self._maintenance: asyncio.Task[None] | None = None
        self._closing: asyncio.Task[None] | None = None
        self._flush_handle: asyncio.Handle | None = None
        self._enabled = False
        self._online = False
        self._ever_connected = False
        self._restoring = True
        self._dirty = True
        self._generation = 0
        self._serial = 0
        self._instance_id: str | None = None
        self._last_error: Exception | None = None

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def connected(self) -> bool:
        return self._online and self._transport.connected

    @property
    def ready(self) -> bool:
        return (
            self.connected
            and not self._restoring
            and all(sub.ready for sub in self._subscriptions.desired)
        )

    @property
    def last_error(self) -> Exception | None:
        return self._last_error

    def subscribe(
        self, source: SubscriptionSource, consumer: SubscriptionConsumer
    ) -> ClientSubscription:
        """Register intent immediately; on_status() reports initialization."""
        for name in (
            "has_cache",
            "apply_snapshot",
            "apply_event",
            "publish",
            "on_status",
        ):
            callback = getattr(consumer, name, None)
            if not callable(callback) or inspect.iscoroutinefunction(callback):
                raise TypeError(f"Consumer {name} must be synchronous")
        previous = self._subscriptions.get(source)
        attached = previous is not None and previous.has_consumer(consumer)
        subscription, _ = self._subscriptions.acquire(source, consumer)
        self._released.discard(subscription)
        if not attached:
            self._schedule_restore()
            self._notify((subscription,))
        return subscription

    def unsubscribe(
        self, source: SubscriptionSource, consumer: SubscriptionConsumer
    ) -> None:
        """Release one consumer; remove the host registration when unused."""
        subscription, unused = self._subscriptions.release(source, consumer)
        if unused:
            self._released.add(subscription)
            self._schedule_restore()
        self._finish_pending()

    def resync(self, subscription: ClientSubscription) -> None:
        """Request fresh snapshots after confirmed data was discarded."""
        if self._subscriptions.get(subscription.source) is not subscription:
            raise ValueError("Subscription is not owned by this client")
        self._recover(RuntimeError("Confirmed subscription cache was lost"))

    async def connect(self) -> None:
        if self._closing is not None and not self._closing.done():
            await asyncio.shield(self._closing)
        self._enabled = True
        if not self.ready:
            self._schedule_restore()
        await self.synchronize()

    async def synchronize(self) -> None:
        """Wait for initialization without cancelling shared work."""
        if not self._enabled:
            raise StoreStateError("Store client is disconnected")
        while not self.ready or self._dirty:
            self._start_maintenance()
            task = self._maintenance
            if task is not None:
                await asyncio.shield(task)
            if not self._enabled:
                raise self._last_error or StoreStateError("Client stopped")

    async def disconnect(self) -> None:
        if self._closing is None or self._closing.done():
            self._enabled = False
            maintenance, flush = self._maintenance, self._flush_task
            self._retire(StoreStateError("Store client disconnected"))
            if maintenance is not None:
                maintenance.cancel()
            self._closing = asyncio.create_task(
                self._close(maintenance, flush)
            )
            self._closing.add_done_callback(self._close_done)
        await asyncio.shield(self._closing)

    async def _close(
        self,
        maintenance: asyncio.Task[None] | None,
        flush: asyncio.Task[None] | None,
    ) -> None:
        try:
            await self._transport.disconnect()
        finally:
            tasks = tuple(t for t in (maintenance, flush) if t is not None)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    def _close_done(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            self._report("Store client cleanup failed", error)

    def set(
        self, *, operation_id: str, schema: str, values: Mapping[str, Any]
    ) -> asyncio.Future[ResponseItem]:
        return self._operation(
            operation_id,
            SetRequest(schema=schema, values=tuple(values.items())),
        )

    def setrow(
        self,
        *,
        operation_id: str,
        schema: str,
        row_id: str,
        values: Mapping[str, Any],
    ) -> asyncio.Future[ResponseItem]:
        return self._operation(
            operation_id,
            SetRowRequest(
                schema=schema, row_id=row_id, values=tuple(values.items())
            ),
        )

    def insert(
        self,
        *,
        operation_id: str,
        schema: str,
        row_id: str,
        values: Mapping[str, Any],
    ) -> asyncio.Future[ResponseItem]:
        return self._operation(
            operation_id,
            InsertRequest(
                schema=schema, row_id=row_id, values=tuple(values.items())
            ),
        )

    def remove(
        self, *, operation_id: str, schema: str, row_id: str
    ) -> asyncio.Future[ResponseItem]:
        return self._operation(
            operation_id, RemoveRequest(schema=schema, row_id=row_id)
        )

    def signal(
        self, *, operation_id: str, schema: str, signal: str, data: Any = None
    ) -> asyncio.Future[ResponseItem]:
        return self._operation(
            operation_id,
            SignalRequest(schema=schema, signal=signal, data=data),
        )

    def signalrow(
        self,
        *,
        operation_id: str,
        schema: str,
        row_id: str,
        signal: str,
        data: Any = None,
    ) -> asyncio.Future[ResponseItem]:
        return self._operation(
            operation_id,
            SignalRowRequest(
                schema=schema, row_id=row_id, signal=signal, data=data
            ),
        )

    def transaction(
        self, *, operation_id: str, mutations: tuple[MutationRequest, ...]
    ) -> asyncio.Future[ResponseItem]:
        return self._submit(
            TransactionRequest(
                request_id=self._new_request_id(),
                operation_id=operation_id,
                mutations=mutations,
            )
        )

    def query(self, query: Query[Any]) -> asyncio.Future[ResponseItem]:
        return self._submit(
            QueryRequest(request_id=self._new_request_id(), query=query)
        )

    def _operation(
        self, operation_id: str, mutation: MutationRequest
    ) -> asyncio.Future[ResponseItem]:
        return self._submit(
            OperationRequest(
                request_id=self._new_request_id(),
                operation_id=operation_id,
                mutation=mutation,
            )
        )

    def _new_request_id(self) -> str:
        return uuid4().hex

    def _schedule_restore(self) -> None:
        self._dirty = True
        self._restoring = True
        self._serial += 1
        self._start_maintenance()

    def _start_maintenance(self) -> None:
        if not self._enabled:
            return
        if self._maintenance is None or self._maintenance.done():
            self._maintenance = asyncio.create_task(self._maintain())
            self._maintenance.add_done_callback(self._maintenance_done)

    def _maintenance_done(self, task: asyncio.Task[None]) -> None:
        if self._maintenance is task:
            self._maintenance = None
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                self._report("Store synchronization failed", error)

    async def _maintain(self) -> None:
        repairs = 0
        while self._enabled:
            generation = self._generation
            try:
                if not self.connected:
                    await self._open_connection()
                    generation = self._generation
                while self._dirty:
                    self._dirty = False
                    await self._restore(generation)
                    self._require_generation(generation)
                messages, self._buffer = self._buffer, []
                try:
                    for index, message in enumerate(messages):
                        self._deliver(message)
                        if self._dirty:
                            self._buffer = messages[index + 1 :] + self._buffer
                            break
                except Exception as error:
                    if repairs:
                        raise
                    repairs += 1
                    self._recover(error)
                    continue
                if self._dirty:
                    continue
                self._restoring = False
                self._last_error = None
                self._notify(self._subscriptions.desired)
                if self._dirty:
                    continue
                self._finish_pending()
                if self._dirty:
                    continue
                return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                lost = not self.connected or generation != self._generation
                if not lost:
                    self._invalidate_all()
                    self._enabled = False
                self._retire(error)
                await self._transport.disconnect()
                if not self._enabled or not self._ever_connected:
                    self._enabled = False
                    raise
                await asyncio.sleep(self._reconnect_delay)

    async def _open_connection(self) -> None:
        await self._transport.disconnect()
        # A fresh transport also isolates delayed Direct transport callbacks.
        self._transport = create_client_transport(self._endpoint)
        self._generation += 1
        generation = self._generation

        async def message(event: str, payload: object | None) -> None:
            await self._handle_message(generation, event, payload)

        async def disconnected() -> None:
            if generation == self._generation:
                self._retire(StoreConnectionError("Store connection was lost"))
                self._start_maintenance()

        await self._transport.connect(message, disconnected)
        if generation != self._generation or not self._transport.connected:
            raise StoreConnectionError(
                "Store connection closed during connect"
            )
        self._online = True
        self._ever_connected = True
        self._dirty = True

    def _require_generation(self, generation: int) -> None:
        if generation != self._generation or not self.connected:
            raise StoreConnectionError("Store connection changed")

    def _retire(self, error: Exception) -> None:
        self._generation += 1
        self._serial += 1
        self._online = False
        self._instance_id = None
        self._restoring = True
        self._dirty = True
        self._last_error = error
        self._buffer.clear()
        self._orphans.clear()
        self._subscriptions.unbind_all()
        for subscription in tuple(self._released):
            if not subscription.consumer_count:
                self._subscriptions.remove(subscription)
        self._released.clear()
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        if self._flush_task is not None:
            if self._flush_task is not asyncio.current_task():
                self._flush_task.cancel()
            self._flush_task = None
        self._queued.clear()
        for request_id in tuple(self._pending):
            self._fail_request(request_id, error)
        self._notify(self._subscriptions.desired, error)

    async def _restore(self, generation: int) -> None:
        serial = self._serial
        subscriptions = self._subscriptions.desired
        released = tuple(s for s in self._released if not s.consumer_count)
        removed_ids = set(self._orphans)
        removed_ids.update(
            sub_id
            for s in released
            if (sub_id := s.subscription_id) is not None
        )
        cleanup = tuple(
            self._submit(UnsubscribeRequest(self._new_request_id(), sub_id))
            for sub_id in removed_ids
            if sub_id is not None
        )
        futures = tuple(
            self._submit(self._subscription_request(s)) for s in subscriptions
        )
        results = await asyncio.gather(*cleanup, *futures)
        self._require_generation(generation)
        for response in results:
            if isinstance(response, FailureResponse):
                raise RuntimeError(response.failure.message)
        self._orphans.difference_update(removed_ids)
        for subscription in released:
            self._subscriptions.unbind(subscription)
            if not subscription.consumer_count:
                self._subscriptions.remove(subscription)
            self._released.discard(subscription)
        snapshots: list[tuple[ClientSubscription, SubscriptionResponse]] = []
        for subscription, result in zip(
            subscriptions, results[len(cleanup) :]
        ):
            response = cast(SubscriptionResponse, result)
            self._check_instance(response.store_instance_id)
            if not subscription.consumer_count:
                self._orphans.add(response.subscription_id)
                self._subscriptions.remove(subscription)
                self._released.discard(subscription)
                self._dirty = True
                continue
            self._subscriptions.bind(
                subscription,
                response.subscription_id,
                response.store_instance_id,
            )
            snapshots.append((subscription, response))
        if serial != self._serial:
            self._dirty = True
            return
        # Validate the entire batch before allowing any consumer to stage it.
        prepared = []
        for subscription, response in snapshots:
            revisions, unchanged = _snapshot_metadata(subscription, response)
            if unchanged and not self._has_cache(subscription):
                self._recover(RuntimeError("Cached snapshot was discarded"))
                return
            subscription.check_snapshot(revisions, unchanged=unchanged)
            prepared.append((subscription, response, revisions, unchanged))
        for subscription, response, _, _ in prepared:
            for consumer in _consumers(subscription):
                copied = _copy_snapshot(response)
                _completed(consumer.apply_snapshot(subscription, copied))
            if not self._has_cache(subscription):
                raise RuntimeError("Consumer did not retain the snapshot")
        for subscription, _, revisions, unchanged in prepared:
            subscription.hydrate(revisions, unchanged=unchanged)
        self._publish(tuple(s for s, _ in snapshots))

    def _subscription_request(
        self, subscription: ClientSubscription
    ) -> RequestType:
        if subscription.cache_valid and not self._has_cache(subscription):
            subscription.invalidate()
        instance, revisions = subscription.cache_hint()
        source = subscription.source
        request_id = self._new_request_id()
        if isinstance(source, Query):
            return QuerySubscriptionRequest(
                request_id, source, instance, revisions
            )
        revision = revisions[source.__name__] if revisions else None
        kind = (
            SingleSubscriptionRequest
            if issubclass(source, SingleSchema)
            else CollectionSubscriptionRequest
        )
        return kind(request_id, source.__name__, instance, revision)

    def _has_cache(self, subscription: ClientSubscription) -> bool:
        for consumer in _consumers(subscription):
            value = consumer.has_cache(subscription)
            if type(value) is not bool:
                _completed(value)
                raise TypeError("Consumer has_cache() must return a bool")
            if not value:
                return False
        return subscription.consumer_count > 0

    def _invalidate_all(self) -> None:
        for subscription in self._subscriptions.desired:
            subscription.invalidate()

    def _recover(self, error: Exception) -> None:
        self._last_error = error
        self._invalidate_all()
        self._buffer.clear()
        self._schedule_restore()
        self._notify(self._subscriptions.desired, error)

    async def _handle_message(
        self, generation: int, event: str, payload: object | None
    ) -> None:
        # No awaits below: receiving, validation, staging, and publication are
        # sequential even if the transport invokes handlers concurrently.
        if generation != self._generation or event != "message":
            return
        try:
            if not isinstance(payload, EventMessage):
                raise TypeError("Store transport returned an invalid event")
            message = _copy_message(payload)
            if self._restoring:
                if len(self._buffer) >= self._max_buffered_events:
                    raise RuntimeError("Initialization event buffer is full")
                self._buffer.append(message)
            else:
                self._deliver(message)
        except Exception as error:
            self._recover(error)

    def _deliver(self, message: EventMessage) -> None:
        commit = message.commit
        self._check_instance(commit.server_instance_id)
        if len(set(message.subscription_ids)) != len(message.subscription_ids):
            raise ValueError("Event contains duplicate subscription IDs")
        raw_schemas = {event.schema for event in commit.events}
        if not raw_schemas <= commit.schema_revisions.keys():
            raise ValueError("Canonical events lack schema revisions")
        query_ids = {event.subscription_id for event in message.query_events}
        if len(query_ids) != len(message.query_events):
            raise ValueError("Event contains duplicate query deltas")
        if not query_ids <= set(message.subscription_ids):
            raise ValueError("Query delta lacks a covered subscription")
        selected = []
        for subscription_id in message.subscription_ids:
            subscription = self._subscriptions.get_by_id(subscription_id)
            if subscription is None or not subscription.consumer_count:
                continue
            if not self._has_cache(subscription):
                raise RuntimeError("Consumer lost confirmed data")
            if not (
                subscription.schema_revisions.keys()
                & commit.schema_revisions.keys()
            ):
                raise ValueError("Event lacks covered subscription revisions")
            if not subscription.check_event(
                subscription_id,
                cast(str, commit.server_instance_id),
                commit.schema_revisions,
            ):
                continue
            source = subscription.source
            if not isinstance(source, Query):
                if source.__name__ not in raw_schemas:
                    raise ValueError("Schema subscription lacks raw events")
                if subscription_id in query_ids:
                    raise ValueError("Schema subscription got a query delta")
                kinds = (
                    (SetEvent,)
                    if issubclass(source, SingleSchema)
                    else (SetRowEvent, InsertEvent, RemoveEvent)
                )
                if any(
                    not isinstance(event, kinds)
                    for event in commit.events
                    if event.schema == source.__name__
                ):
                    raise ValueError("Event type does not match its schema")
            selected.append(subscription)
        subscriptions = tuple(selected)
        for consumer, covered in _groups(subscriptions):
            filtered = _filter_message(message, covered)
            _completed(consumer.apply_event(_copy_message(filtered), covered))
        for subscription in subscriptions:
            if not self._has_cache(subscription):
                raise RuntimeError("Consumer lost data during application")
        for subscription in subscriptions:
            subscription.advance(
                cast(str, subscription.subscription_id),
                cast(str, commit.server_instance_id),
                commit.schema_revisions,
            )
        self._publish(subscriptions)
        self._finish_pending()

    def _publish(self, subscriptions: tuple[ClientSubscription, ...]) -> None:
        for consumer, covered in _groups(subscriptions):
            _completed(consumer.publish(covered))

    def _notify(
        self,
        subscriptions: tuple[ClientSubscription, ...],
        error: Exception | None = None,
    ) -> None:
        for subscription in subscriptions:
            for consumer in _consumers(subscription):
                try:
                    _completed(consumer.on_status(subscription, error))
                except Exception as failure:
                    self._report("Store consumer status failed", failure)

    def _report(self, message: str, error: BaseException) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.call_exception_handler(
            {"message": message, "exception": error, "client": self}
        )

    def _check_instance(self, instance_id: str | None) -> None:
        if not isinstance(instance_id, str) or not instance_id:
            raise ValueError("Store message lacks an instance ID")
        if self._instance_id is not None and self._instance_id != instance_id:
            raise ValueError("Store instance changed on this connection")
        self._instance_id = instance_id

    def _submit(self, request: RequestType) -> asyncio.Future[ResponseItem]:
        if not self.connected:
            raise StoreStateError("Store client is not connected")
        if request.request_id in self._pending:
            raise ValueError("Store request ID is already pending")
        request = deepcopy(request)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ResponseItem] = loop.create_future()
        timer = loop.call_later(
            self._request_timeout, self._expire, request.request_id
        )
        self._pending[request.request_id] = _Pending(
            future, self._subscriptions.desired, timer
        )
        future.add_done_callback(
            lambda done: self._forget(request.request_id, done)
        )
        self._queued.append(request)
        self._schedule_flush()
        return future

    def _forget(
        self, request_id: str, future: asyncio.Future[ResponseItem]
    ) -> None:
        pending = self._pending.get(request_id)
        if pending is not None and pending.future is future and future.done():
            self._pending.pop(request_id)
            pending.timer.cancel()

    def _expire(self, request_id: str) -> None:
        pending = self._pending.get(request_id)
        if pending is None or pending.future.done():
            return
        error = StoreTimeoutError(
            "Store response or local synchronization timed out; "
            "an interrupted write may already have committed"
        )
        self._invalidate_all()
        self._retire(error)
        self._start_maintenance()

    def _schedule_flush(self) -> None:
        if self._flush_handle is not None or self._flush_task is not None:
            return
        if self._queued and self.connected:
            self._flush_handle = asyncio.get_running_loop().call_soon(
                self._launch_flush, self._generation
            )

    def _launch_flush(self, generation: int) -> None:
        self._flush_handle = None
        if generation != self._generation or not self.connected:
            return
        self._flush_task = asyncio.create_task(self._flush(generation))

    async def _flush(self, generation: int) -> None:
        items = tuple(
            item
            for item in self._queued
            if item.request_id in self._pending
            and not self._pending[item.request_id].future.done()
        )
        self._queued.clear()
        try:
            if items:
                response = await self._transport.request(Request(items))
                self._require_generation(generation)
                self._settle_responses(items, response)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if generation == self._generation:
                self._invalidate_all()
                self._retire(error)
                self._start_maintenance()
        finally:
            if self._flush_task is asyncio.current_task():
                self._flush_task = None
                self._schedule_flush()

    def _settle_responses(
        self, requests: tuple[RequestType, ...], response: Response
    ) -> None:
        if not isinstance(response, Response):
            raise TypeError("Transport returned an invalid response batch")
        expected = {request.request_id: request for request in requests}
        received = [item.request_id for item in response.items]
        if len(received) != len(expected) or set(received) != expected.keys():
            raise ValueError("Response IDs do not match the request batch")
        for item in response.items:
            _validate_response(expected[item.request_id], item)
            instance = getattr(item, "store_instance_id", None)
            if instance is not None:
                self._check_instance(instance)
        for item in response.items:
            pending = self._pending.get(item.request_id)
            if pending is not None:
                pending.response = item
        self._finish_pending()

    def _finish_pending(self) -> None:
        for request_id, pending in tuple(self._pending.items()):
            response = pending.response
            if response is None or pending.future.done():
                continue
            if isinstance(response, _WRITE_RESPONSES):
                if self._restoring:
                    continue
                if not self._covers(pending, response):
                    continue
            self._pending.pop(request_id, None)
            pending.timer.cancel()
            pending.future.set_result(response)

    def _covers(self, pending: _Pending, response: _WriteResponse) -> bool:
        for subscription in pending.subscriptions:
            if not subscription.consumer_count:
                continue
            source = subscription.source
            if (
                not isinstance(source, Query)
                and source.__name__ not in response.schema_revisions
            ):
                continue
            if not subscription.ready:
                return False
            if subscription.store_instance_id != response.store_instance_id:
                return False
            current = subscription.schema_revisions
            relevant = current.keys() & response.schema_revisions.keys()
            if relevant and not self._has_cache(subscription):
                self._recover(RuntimeError("Confirmed cache was lost"))
                return False
            if any(
                current[name] < response.schema_revisions[name]
                for name in relevant
            ):
                return False
        return True

    def _fail_request(self, request_id: str, error: Exception) -> None:
        pending = self._pending.pop(request_id, None)
        if pending is not None:
            pending.timer.cancel()
            if not pending.future.done():
                pending.future.set_exception(error)


def _consumers(
    subscription: ClientSubscription,
) -> tuple[SubscriptionConsumer, ...]:
    return cast(tuple[SubscriptionConsumer, ...], subscription.consumers)


def _groups(
    subscriptions: tuple[ClientSubscription, ...],
) -> tuple[tuple[SubscriptionConsumer, tuple[ClientSubscription, ...]], ...]:
    groups: dict[
        int, tuple[SubscriptionConsumer, list[ClientSubscription]]
    ] = {}
    for subscription in subscriptions:
        for consumer in _consumers(subscription):
            group = groups.setdefault(id(consumer), (consumer, []))
            group[1].append(subscription)
    return tuple((owner, tuple(subs)) for owner, subs in groups.values())


def _completed(result: object) -> None:
    if inspect.iscoroutine(result):
        result.close()
    if result is not None:
        raise TypeError("Consumer callbacks must return None synchronously")


def _snapshot_metadata(
    subscription: ClientSubscription, response: SubscriptionResponse
) -> tuple[dict[str, int], bool]:
    source = subscription.source
    if isinstance(source, Query):
        if not isinstance(response, QuerySubscriptionResponse):
            raise TypeError("Query subscription received a schema snapshot")
        shape = response.shape
        if (
            shape.root_schema != source.schema_name
            or not shape.live_compatible
        ):
            raise ValueError("Query snapshot shape does not match its source")
        if len(shape.joins) != len(source.joins):
            raise ValueError("Query snapshot has different joins")
        for requested, actual in zip(source.joins, shape.joins):
            if (
                requested.path.parts[:-1] != actual.owner_path
                or requested.kind != actual.kind
                or (requested.alias and requested.alias != actual.output_name)
            ):
                raise ValueError("Query snapshot has a different join shape")
        dependencies = {shape.root_schema}
        dependencies.update(join.target_schema for join in shape.joins)
        if response.schema_revisions.keys() != dependencies:
            raise ValueError("Query snapshot lacks exact dependency revisions")
        return response.schema_revisions, response.rows is None
    if isinstance(response, SingleSubscriptionResponse):
        return {source.__name__: response.revision}, response.values is None
    if isinstance(response, CollectionSubscriptionResponse):
        return {source.__name__: response.revision}, response.rows is None
    raise TypeError("Schema subscription received a query snapshot")


def _filter_message(
    message: EventMessage, subscriptions: tuple[ClientSubscription, ...]
) -> EventMessage:
    raw_schemas = {
        s.source.__name__
        for s in subscriptions
        if not isinstance(s.source, Query)
    }
    dependencies = {name for s in subscriptions for name in s.schema_revisions}
    ids = tuple(cast(str, s.subscription_id) for s in subscriptions)
    commit = replace(
        message.commit,
        events=tuple(
            e for e in message.commit.events if e.schema in raw_schemas
        ),
        schema_revisions={
            name: value
            for name, value in message.commit.schema_revisions.items()
            if name in dependencies
        },
    )
    return EventMessage(
        commit,
        ids,
        tuple(e for e in message.query_events if e.subscription_id in ids),
    )


def _validate_response(request: RequestType, response: ResponseItem) -> None:
    if isinstance(response, FailureResponse):
        return
    required: set[str] = set()
    row_id: str | None = None
    operation_id: str | None = None
    if isinstance(request, OperationRequest):
        mutation = request.mutation
        operation_id = request.operation_id
        kinds = {
            SetRequest: SetResponse,
            SetRowRequest: SetRowResponse,
            InsertRequest: InsertResponse,
            RemoveRequest: RemoveResponse,
            SignalRequest: SignalResponse,
            SignalRowRequest: SignalResponse,
        }
        expected = kinds[type(mutation)]
        required.add(mutation.schema)
        if isinstance(mutation, (SetRowRequest, InsertRequest, RemoveRequest)):
            row_id = mutation.row_id
    elif isinstance(request, TransactionRequest):
        expected = TransactionResponse
        operation_id = request.operation_id
        required.update(mutation.schema for mutation in request.mutations)
    else:
        expected = {
            QueryRequest: QueryResponse,
            SingleSubscriptionRequest: SingleSubscriptionResponse,
            CollectionSubscriptionRequest: CollectionSubscriptionResponse,
            QuerySubscriptionRequest: QuerySubscriptionResponse,
            UnsubscribeRequest: UnsubscribeResponse,
        }[type(request)]
    if not isinstance(response, expected):
        raise TypeError("Store response type does not match its request")
    if (
        operation_id is not None
        and getattr(response, "operation_id") != operation_id
    ):
        raise ValueError("Store response operation ID does not match")
    if row_id is not None and getattr(response, "row_id") != row_id:
        raise ValueError("Store response row ID does not match")
    if isinstance(request, UnsubscribeRequest):
        actual_id = cast(UnsubscribeResponse, response).subscription_id
        if actual_id != request.subscription_id:
            raise ValueError("Unsubscribe response ID does not match")
    if isinstance(response, _WRITE_RESPONSES):
        revisions = response.schema_revisions
        if not required <= revisions.keys():
            raise ValueError("Write response lacks requested schema revisions")
        for name, revision in revisions.items():
            if not isinstance(name, str) or not name:
                raise ValueError("Invalid schema name in write response")
            if type(revision) is not int or revision < 0:
                raise ValueError("Invalid schema revision in write response")


def _copy_rows(rows: QueryResult) -> QueryResult:
    return tuple(QueryRow(row.id, deepcopy(dict(row))) for row in rows)


def _copy_snapshot(response: SubscriptionResponse) -> SubscriptionResponse:
    if isinstance(response, SingleSubscriptionResponse):
        return replace(response, values=deepcopy(response.values))
    rows = _copy_rows(response.rows) if response.rows is not None else None
    if isinstance(response, QuerySubscriptionResponse):
        return replace(
            response,
            rows=rows,
            schema_revisions=dict(response.schema_revisions),
        )
    return replace(response, rows=rows)


def _copy_message(message: EventMessage) -> EventMessage:
    return replace(
        message,
        commit=deepcopy(message.commit),
        query_events=tuple(
            replace(event, rows=_copy_rows(event.rows))
            for event in message.query_events
        ),
    )
