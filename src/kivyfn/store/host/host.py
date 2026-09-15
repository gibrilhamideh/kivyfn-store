# src/kivyfn/store/host/host.py
from __future__ import annotations

import asyncio
from collections.abc import Callable
from logging import getLogger as get_logger

from kivyfn.store.error import StoreInternalError, StoreStateError
from kivyfn.store.events import StoreCommit
from kivyfn.store.host.client import HostClients
from kivyfn.store.host.query import HostQueries
from kivyfn.store.host.request import HostRequests
from kivyfn.store.host.subscription import HostSubscriptions
from kivyfn.store.protocol.message.requests import RequestBatch
from kivyfn.store.protocol.message.responses import ResponseBatch
from kivyfn.store.store import Store
from kivyfn.store.transport.endpoint import create_host_transport

logger = get_logger(__name__)


class StoreHost:
    """Expose one Store through a transport."""

    def __init__(self, store: Store, endpoint: str) -> None:
        self.store = store
        self.endpoint = endpoint

        self.transport = create_host_transport(endpoint)

        self.queries = HostQueries(store)
        self.subscriptions = HostSubscriptions(store, self.queries)
        self.requests = HostRequests(store, self.subscriptions)

        self.clients = HostClients(
            self.transport.emit,
            self.subscriptions.prepare,
            self._on_client_error,
        )

        self._running = False
        self._lifecycle = asyncio.Lock()
        self._unsubscribe: Callable[[], None] | None = None
        self._closed: asyncio.Future[BaseException | None] | None = None
        self._failure: BaseException | None = None
        self._failure_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._client_closures: set[asyncio.Task[None]] = set()

    @property
    def running(self) -> bool:
        return self._running

    async def start(self) -> None:
        closing = self._close_task
        if closing is not None:
            await asyncio.shield(closing)

        async with self._lifecycle:
            if self._running:
                return

            self._closed = asyncio.get_running_loop().create_future()
            self._failure = None
            self._close_task = None
            try:
                self._unsubscribe = self.store.subscribe_commits(
                    self._on_commit
                )
                await self.transport.start(self)
                self._running = True
            except BaseException as error:
                self._failure = error
                try:
                    await self._close()
                finally:
                    self._finish_close()
                raise

    async def stop(self) -> None:
        await asyncio.shield(self._begin_close())

    def _begin_close(self) -> asyncio.Task[None]:
        task = self._close_task
        if task is None or task.done():
            task = asyncio.create_task(self._close_serialized())
            self._close_task = task
            task.add_done_callback(self._observe_close)
        return task

    async def _close_serialized(self) -> None:
        async with self._lifecycle:
            try:
                await self._close()
            except BaseException as error:
                if self._failure is None:
                    self._failure = error
                raise
            finally:
                self._finish_close()

    @staticmethod
    def _observe_close(task: asyncio.Task[None]) -> None:
        # A cancelled stop waiter must not leave a failed task unobserved.
        if not task.cancelled():
            task.exception()

    async def wait_closed(self) -> None:
        closed = self._closed

        if closed is None:
            raise StoreStateError("StoreHost has not been started")

        failure = await asyncio.shield(closed)

        if failure is not None:
            raise StoreInternalError(
                "StoreHost stopped after an internal failure"
            ) from failure

    async def connect(self, client_id: str) -> None:
        self._require_running()
        self.clients.add(client_id)

    async def request(
        self, client_id: str, request: RequestBatch
    ) -> ResponseBatch:
        self._require_running()
        if self.clients.get(client_id) is None:
            raise StoreStateError("Store client is not registered")
        response = self.requests.handle(client_id, request)
        await self.clients.flush(client_id)
        return response

    async def disconnect(self, client_id: str) -> None:
        self.subscriptions.remove_client(client_id)
        await self.clients.remove(client_id)

    def _require_running(self) -> None:
        if not self._running:
            raise StoreStateError("StoreHost is stopped")

    async def _close(self) -> None:
        self._running = False

        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

        self.subscriptions.clear()

        try:
            await self.clients.clear()

        finally:
            try:
                await self.transport.stop()
            finally:
                pending = tuple(self._client_closures)
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)

    def _finish_close(self) -> None:
        closed = self._closed

        if closed is not None and not closed.done():
            closed.set_result(self._failure)

    def _on_commit(self, commit: StoreCommit) -> None:
        if not self._running:
            return

        try:
            messages = self.subscriptions.capture(
                commit, on_query_error=self._on_client_error
            )
            for client_id, message in messages:
                self.clients.enqueue(client_id, message)

        except Exception as error:
            logger.exception("Store subscription capture failed")
            self._fail(error)

    def _on_client_error(self, client_id: str, error: Exception) -> None:
        logger.error(
            "Store event delivery to %s failed", client_id, exc_info=error
        )

        task = asyncio.create_task(self._disconnect_failed_client(client_id))
        self._client_closures.add(task)
        task.add_done_callback(self._client_closures.discard)

    async def _disconnect_failed_client(self, client_id: str) -> None:
        try:
            await self.transport.disconnect(client_id)
        except Exception:
            logger.exception("Failed to disconnect Store client %s", client_id)
        finally:
            await self.disconnect(client_id)

    def _fail(self, error: Exception) -> None:
        if self._failure is not None:
            return

        self._failure = error
        self._running = False
        closing = self._begin_close()

        if self._failure_task is None:
            self._failure_task = asyncio.create_task(
                self._close_failed(closing)
            )

    async def _close_failed(self, closing: asyncio.Task[None]) -> None:
        try:
            await asyncio.shield(closing)

        except Exception:
            logger.exception(
                "StoreHost failed to close after an internal failure"
            )

        finally:
            self._failure_task = None
