# src/kivyfn/store/host/host.py
from __future__ import annotations

import asyncio

from collections.abc import Callable

from kivyfn.logging import get_logger
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
        self._unsubscribe: Callable[[], None] | None = None
        self._closed: asyncio.Future[BaseException | None] | None = None
        self._failure: Exception | None = None
        self._failure_task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._running

    async def start(self) -> None:
        if self._running:
            return

        loop = asyncio.get_running_loop()

        self._closed = loop.create_future()
        self._failure = None
        self._running = True

        self._unsubscribe = self.store.subscribe_commits(self._on_commit)

        try:
            await self.transport.start(self)

        except BaseException:
            await self._close()
            raise

    async def stop(self) -> None:
        await self._close()

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
        return self.requests.handle(client_id, request)

    async def disconnect(self, client_id: str) -> None:
        self.subscriptions.remove_client(client_id)
        await self.clients.remove(client_id)

    def _require_running(self) -> None:
        if not self._running:
            raise StoreStateError("StoreHost is stopped")

    async def _close(self) -> None:
        if not self._running and self._unsubscribe is None:
            self._finish_close()
            return

        self._running = False

        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

        self.subscriptions.clear()

        try:
            await self.clients.clear()

        finally:
            await self.transport.stop()
            self._finish_close()

    def _finish_close(self) -> None:
        closed = self._closed

        if closed is not None and not closed.done():
            closed.set_result(self._failure)

    def _on_commit(self, commit: StoreCommit) -> None:
        if not self._running:
            return

        try:
            for client_id, message in self.subscriptions.capture(commit):
                self.clients.enqueue(client_id, message)

        except Exception as error:
            logger.exception("Store subscription capture failed")
            self._fail(error)

    def _on_client_error(self, client_id: str, error: Exception) -> None:
        logger.error(
            "Store event delivery to %s failed", client_id, exc_info=error
        )

        self._fail(error)

    def _fail(self, error: Exception) -> None:
        if self._failure is not None:
            return

        self._failure = error
        self._running = False

        if self._failure_task is None:
            self._failure_task = asyncio.create_task(self._close_failed())

    async def _close_failed(self) -> None:
        try:
            await self._close()

        except Exception:
            logger.exception(
                "StoreHost failed to close after an internal failure"
            )

        finally:
            self._failure_task = None
