# src/kivyfn/store/client/request.py
"""Outgoing request batches, response matching, and completion deadlines."""

from __future__ import annotations

import asyncio

from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import TypeAlias, cast

from kivyfn.store.protocol.message.requests import (
    CollectionSubscriptionRequest,
    InsertRequest,
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


WriteResponse: TypeAlias = (
    SetResponse
    | SetRowResponse
    | InsertResponse
    | RemoveResponse
    | TransactionResponse
)
RequestSender: TypeAlias = Callable[[Request], Awaitable[Response]]
RequestFailureHandler: TypeAlias = Callable[[Exception], None]
StoreInstanceChecker: TypeAlias = Callable[[str], None]
WriteCompletion: TypeAlias = Callable[[WriteResponse], bool]

_WRITE_RESPONSES = (
    SetResponse,
    SetRowResponse,
    InsertResponse,
    RemoveResponse,
    TransactionResponse,
)
_SUBSCRIPTION_RESPONSES = (
    SingleSubscriptionResponse,
    CollectionSubscriptionResponse,
    QuerySubscriptionResponse,
)


@dataclass(slots=True)
class _Pending:
    request: RequestType
    future: asyncio.Future[ResponseItem]
    ready: WriteCompletion | None
    timer: asyncio.TimerHandle | None = None
    response: ResponseItem | None = None


class ClientRequests:
    """Batch requests and complete their futures on the client's event loop.

    Runtime binds the connected sender, checks Store identity, and handles
    failures. Transport supplies valid typed messages; wire validation belongs
    to the codec. This handler checks their relation to outstanding requests.

    Subscription handling supplies synchronous ready() callbacks and calls
    advance() after publishing confirmed data. Timeouts cover queueing,
    delivery, and that completion wait. Interrupted writes are never resent.
    """

    def __init__(
        self,
        *,
        on_failure: RequestFailureHandler,
        check_instance: StoreInstanceChecker,
        timeout: float = 10.0,
    ) -> None:
        self._on_failure = on_failure
        self._check_instance = check_instance
        self._timeout = timeout
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sender: RequestSender | None = None
        self._binding: object | None = None
        self._queued: list[_Pending] = []
        self._pending: dict[str, _Pending] = {}
        self._flush_handle: asyncio.Handle | None = None
        self._flush_task: asyncio.Task[None] | None = None

    @property
    def bound(self) -> bool:
        return self._binding is not None

    @property
    def pending_count(self) -> int:
        return sum(not p.future.done() for p in self._pending.values())

    @property
    def queued_count(self) -> int:
        return sum(self._is_pending(pending) for pending in self._queued)

    def bind(self, sender: RequestSender) -> None:
        """Accept requests through a newly connected transport."""
        if self.bound:
            raise RuntimeError("Unbind the previous connection first")
        self._loop = asyncio.get_running_loop()
        self._sender = sender
        self._binding = object()

    def unbind(
        self, error: Exception | None = None
    ) -> asyncio.Task[None] | None:
        """Fail unfinished work and return the cancelled send task, if any.

        Runtime may await that task after closing its transport. Retirement
        is synchronous and does not wait for network or cancellation cleanup.
        Calling this directly does not invoke on_failure().
        """
        if error is None:
            error = ConnectionError("Store client is disconnected")
        self._binding = None
        self._sender = None
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        task, self._flush_task = self._flush_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        self._queued.clear()
        pending, self._pending = tuple(self._pending.values()), {}
        for entry in pending:
            self._cancel_timer(entry)
            if not entry.future.done():
                entry.future.set_exception(error)
        return task

    def submit(
        self, request: RequestType, *, ready: WriteCompletion | None = None
    ) -> asyncio.Future[ResponseItem]:
        """Queue one detached request; never queue while disconnected.

        For writes from State, supply the subscription handler's completion
        check. Without one, a successful response completes immediately.
        Failure responses and non-write responses never wait for ready().
        Cancelling a queued future skips sending; cancelling a sent future
        does not undo the Store operation or cancel other batched requests.
        """
        loop, binding = self._loop, self._binding
        if loop is None or binding is None:
            raise ConnectionError("Store request handler is not connected")
        previous = self._pending.get(request.request_id)
        if previous is not None:
            if not previous.future.done():
                raise ValueError("Store request ID is already pending")
            self._forget(previous)
        request = deepcopy(request)
        future: asyncio.Future[ResponseItem] = loop.create_future()
        pending = _Pending(request, future, ready)
        pending.timer = loop.call_later(
            self._timeout, self._expire, binding, pending
        )
        self._pending[request.request_id] = pending
        self._queued.append(pending)
        future.add_done_callback(lambda done: self._forget(pending))
        self._schedule_flush()
        return future

    def advance(self) -> None:
        """Recheck successful writes after subscription progress or release."""
        binding = self._binding
        if binding is None:
            return
        try:
            for pending in tuple(self._pending.values()):
                response = pending.response
                if response is None or not self._is_pending(pending):
                    continue
                if (
                    isinstance(response, _WRITE_RESPONSES)
                    and pending.ready is not None
                    and not pending.ready(response)
                ):
                    continue
                # A callback may have retired this binding or cancelled work.
                if self._binding is not binding:
                    return
                if not self._is_pending(pending):
                    continue
                self._pending.pop(pending.request.request_id)
                self._cancel_timer(pending)
                pending.future.set_result(response)
        except Exception as error:
            self._fail(binding, error)

    def _is_pending(self, pending: _Pending) -> bool:
        return (
            self._pending.get(pending.request.request_id) is pending
            and not pending.future.done()
        )

    def _forget(self, pending: _Pending) -> None:
        if not pending.future.done():
            return
        if self._pending.get(pending.request.request_id) is pending:
            self._pending.pop(pending.request.request_id)
        self._cancel_timer(pending)

    @staticmethod
    def _cancel_timer(pending: _Pending) -> None:
        if pending.timer is not None:
            pending.timer.cancel()
            pending.timer = None

    def _expire(self, binding: object, pending: _Pending) -> None:
        if self._binding is binding and self._is_pending(pending):
            self._fail(
                binding,
                TimeoutError(
                    "Store response or local synchronization timed out; "
                    "a sent write may already have committed"
                ),
            )

    def _schedule_flush(self) -> None:
        if self._flush_handle is not None or self._flush_task is not None:
            return
        if self._queued and self._loop is not None and self.bound:
            self._flush_handle = self._loop.call_soon(
                self._launch_flush, self._binding
            )

    def _launch_flush(self, binding: object) -> None:
        if self._binding is not binding:
            return
        self._flush_handle = None
        sender = self._sender
        if sender is not None:
            self._flush_task = asyncio.create_task(
                self._flush(binding, sender)
            )

    async def _flush(self, binding: object, sender: RequestSender) -> None:
        pending = tuple(p for p in self._queued if self._is_pending(p))
        self._queued.clear()
        try:
            if pending:
                request = Request(tuple(p.request for p in pending))
                response = await sender(request)
                if self._binding is binding:
                    self._settle(binding, pending, response)
        except asyncio.CancelledError:
            self._fail(binding, ConnectionError("Store request send stopped"))
            raise
        except Exception as error:
            self._fail(binding, error)
        finally:
            if self._flush_task is asyncio.current_task():
                self._flush_task = None
                self._schedule_flush()

    def _settle(
        self,
        binding: object,
        pending: tuple[_Pending, ...],
        response: Response,
    ) -> None:
        expected = {entry.request.request_id: entry for entry in pending}
        received = {item.request_id for item in response.items}
        if received != expected.keys():
            raise ValueError("Response IDs do not match the request batch")
        # Match the complete batch before resolving any of its futures.
        for item in response.items:
            _match_response(expected[item.request_id].request, item)
            if isinstance(
                item,
                (*_WRITE_RESPONSES, *_SUBSCRIPTION_RESPONSES, SignalResponse),
            ):
                self._check_instance(item.store_instance_id)
                if self._binding is not binding:
                    return
        for item in response.items:
            entry = expected[item.request_id]
            if self._is_pending(entry):
                entry.response = item
        self.advance()

    def _fail(self, binding: object, error: Exception) -> None:
        if self._binding is not binding:
            return
        self.unbind(error)
        try:
            self._on_failure(error)
        except Exception as failure:
            if self._loop is not None:
                self._loop.call_exception_handler(
                    {
                        "message": "Store request failure handler failed",
                        "exception": failure,
                        "request_error": error,
                    }
                )


def _match_response(request: RequestType, response: ResponseItem) -> None:
    if isinstance(response, FailureResponse):
        return
    required: set[str] = set()
    row_id: str | None = None
    operation_id: str | None = None
    if isinstance(request, OperationRequest):
        mutation = request.mutation
        operation_id = request.operation_id
        expected = {
            SetRequest: SetResponse,
            SetRowRequest: SetRowResponse,
            InsertRequest: InsertResponse,
            RemoveRequest: RemoveResponse,
            SignalRequest: SignalResponse,
            SignalRowRequest: SignalResponse,
        }[type(mutation)]
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
        if not required <= response.schema_revisions.keys():
            raise ValueError("Write response lacks requested schema revisions")
