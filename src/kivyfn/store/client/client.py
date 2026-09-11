# src/kivyfn/store/client/client.py
from __future__ import annotations

import asyncio

from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from kivyfn.store.protocol.message.requests import (
    InsertRequest,
    MutationRequest,
    OperationRequest,
    QueryRequest,
    RemoveRequest,
    Request,
    RequestType,
    SetRequest,
    SetRowRequest,
    SignalRequest,
    SignalRowRequest,
    TransactionRequest,
)
from kivyfn.store.protocol.message.responses import Response, ResponseItem
from kivyfn.store.query import Query
from kivyfn.store.transport.endpoint import create_client_transport


_MESSAGE_EVENT = "message"


class StoreClient:
    """Async connection coordinator for one Store."""

    def __init__(self, endpoint: str) -> None:
        self._endpoint = endpoint
        self._transport = create_client_transport(endpoint)

        self._loop: asyncio.AbstractEventLoop | None = None

        self._queued: list[RequestType] = []
        self._pending: dict[str, asyncio.Future[ResponseItem]] = {}

        self._flush_scheduled = False
        self._flush_running = False
        self._flush_task: asyncio.Task[None] | None = None

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def connected(self) -> bool:
        return self._transport.connected

    async def connect(self) -> None:
        if self.connected:
            return

        self._loop = asyncio.get_running_loop()

        try:
            await self._transport.connect(
                self._handle_message, self._handle_disconnect
            )
        except BaseException:
            self._loop = None
            raise

    async def disconnect(self) -> None:
        if not self.connected:
            self._fail_pending(ConnectionError("Store client is disconnected"))
            self._loop = None
            return

        task = self._flush_task

        if task is not None:
            task.cancel()

            await asyncio.gather(task, return_exceptions=True)

        try:
            await self._transport.disconnect()
        finally:
            self._flush_task = None
            self._flush_running = False
            self._flush_scheduled = False
            self._loop = None

            self._fail_pending(ConnectionError("Store client disconnected"))

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

    def _submit(self, request: RequestType) -> asyncio.Future[ResponseItem]:
        if not self.connected:
            raise RuntimeError("Store client is not connected")

        loop = self._loop

        if loop is None:
            raise RuntimeError("Store client has no running event loop")

        if request.request_id in self._pending:
            raise ValueError(
                f"Store request ID is already pending: {request.request_id!r}"
            )

        future = loop.create_future()

        self._queued.append(request)
        self._pending[request.request_id] = future

        self._schedule_flush()

        return future

    def _schedule_flush(self) -> None:
        if self._flush_scheduled or self._flush_running or not self._queued:
            return

        loop = self._loop

        if loop is None:
            return

        self._flush_scheduled = True

        loop.call_soon(self._launch_flush)

    def _launch_flush(self) -> None:
        self._flush_scheduled = False

        if self._flush_running or not self.connected or not self._queued:
            return

        self._flush_running = True
        self._flush_task = asyncio.create_task(self._flush())

    async def _flush(self) -> None:
        items = tuple(self._queued)
        self._queued.clear()

        request_ids = tuple(item.request_id for item in items)

        try:
            response = await self._transport.request(Request(items=items))
            self._settle_responses(request_ids, response)
        except asyncio.CancelledError:
            self._fail_requests(
                request_ids, ConnectionError("Store request was cancelled")
            )
            raise
        except BaseException as error:
            self._fail_requests(request_ids, error)
        finally:
            self._flush_task = None
            self._flush_running = False

            if self._queued:
                self._schedule_flush()

    def _settle_responses(
        self, request_ids: tuple[str, ...], response: Response
    ) -> None:
        expected = set(request_ids)

        for item in response.items:
            request_id = item.request_id

            if request_id not in expected:
                continue

            expected.remove(request_id)

            future = self._pending.pop(request_id, None)

            if future is None or future.done():
                continue

            future.set_result(item)

        for request_id in expected:
            future = self._pending.pop(request_id, None)

            if future is None or future.done():
                continue

            future.set_exception(
                RuntimeError(
                    "Store host returned no response for request "
                    f"{request_id!r}"
                )
            )

    def _fail_requests(
        self, request_ids: tuple[str, ...], error: BaseException
    ) -> None:
        for request_id in request_ids:
            future = self._pending.pop(request_id, None)

            if future is None or future.done():
                continue

            future.set_exception(error)

    def _fail_pending(self, error: BaseException) -> None:
        self._queued.clear()

        pending = tuple(self._pending.values())
        self._pending.clear()

        for future in pending:
            if not future.done():
                future.set_exception(error)

    async def _handle_message(
        self, event: str, _message: object | None
    ) -> None:
        if event != _MESSAGE_EVENT:
            return

    async def _handle_disconnect(self) -> None:
        self._flush_running = False
        self._flush_scheduled = False
        self._flush_task = None

        self._fail_pending(ConnectionError("Store connection was lost"))
