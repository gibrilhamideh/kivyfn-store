# src/kivyfn/store/host/runtime.py
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING

from kivyfn.logging import get_logger
from kivyfn.store.error import StoreInternalError, StoreStateError
from kivyfn.store.events import StoreCommit
from kivyfn.store.host.client import HostClients

if TYPE_CHECKING:
    from kivyfn.store.host.host import StoreHost
    from kivyfn.store.host.request import HostRequests
    from kivyfn.store.host.subscription import HostSubscriptions
    from kivyfn.store.protocol.message.requests import Request
    from kivyfn.store.protocol.message.responses import Response
    from kivyfn.store.store import Store
    from kivyfn.store.transport.host import HostTransport


logger = get_logger(__name__)


class HostRuntime:
    """Own the host lifetime, connected clients, and commit-feed delivery.

    Capture runs synchronously on the committing thread. HostClients queues
    the captured messages for transport I/O on the host event loop.
    """

    def __init__(
        self,
        store: Store,
        transport: HostTransport,
        requests: HostRequests,
        subscriptions: HostSubscriptions,
    ) -> None:
        self._store = store
        self._transport = transport
        self._requests = requests
        self._subscriptions = subscriptions
        self.clients = HostClients(
            transport.emit, subscriptions.prepare, self._on_client_error
        )
        self._lifecycle = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._generation = object()
        self._closed: asyncio.Future[BaseException | None] | None = None
        self._failure: Exception | None = None
        self._failure_task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._loop is not None and self._transport.running

    async def start(self, host: StoreHost) -> None:
        async with self._lifecycle:
            if self.running:
                return

            if self._unsubscribe is not None:
                await self._close()

            loop = asyncio.get_running_loop()
            self._closed = loop.create_future()
            self._failure = None

            try:
                with self._store.synchronized():
                    self._generation = object()
                    self._loop = loop
                    self._unsubscribe = self._store.subscribe_commits(
                        self._on_commit
                    )

                await self._transport.start(host)
            except BaseException:
                await self._close()
                raise

    async def stop(self) -> None:
        async with self._lifecycle:
            await self._close()

    async def wait_closed(self) -> None:
        """Wait for this host generation to stop.

        A normal stop returns normally. An internal runtime failure is raised
        after cleanup so a process owner can terminate instead of remaining
        ready with a stopped host.
        """
        closed = self._closed
        if closed is None:
            raise StoreStateError("StoreHost has not been started")

        failure = await asyncio.shield(closed)
        if failure is not None:
            raise StoreInternalError(
                "StoreHost stopped after an internal failure"
            ) from failure

    def connect(self, client_id: str) -> None:
        with self._store.synchronized():
            self._require_running()
            self.clients.add(client_id)

    def request(self, client_id: str, request: Request) -> Response:
        with self._store.synchronized():
            self._require_running()
            return self._requests.handle(client_id, request)

    async def disconnect(self, client_id: str) -> None:
        self._subscriptions.remove_client(client_id)
        await self.clients.remove(client_id)

    def _require_running(self) -> None:
        if self._loop is None:
            raise StoreStateError("StoreHost is stopped")

    async def _close(self) -> None:
        failure: BaseException | None = self._failure
        try:
            with self._store.synchronized():
                self._loop = None

                if self._unsubscribe is not None:
                    self._unsubscribe()
                    self._unsubscribe = None

                self._subscriptions.clear()

            try:
                await self.clients.clear()
            finally:
                await self._transport.stop()
        except BaseException as error:
            if failure is None:
                failure = error
            raise
        finally:
            closed = self._closed
            if closed is not None and not closed.done():
                closed.set_result(failure)

    def _on_commit(self, commit: StoreCommit) -> None:
        """The Store invokes this callback while holding its lock."""
        if self._loop is None:
            return

        try:
            for client_id, message in self._subscriptions.capture(commit):
                self.clients.enqueue(client_id, message)
        except Exception as error:
            logger.exception("Store subscription capture failed")
            self._fail(error)

    def _on_client_error(self, client_id: str, error: Exception) -> None:
        logger.error(
            "Store event delivery to %s failed", client_id, exc_info=error
        )
        with self._store.synchronized():
            self._fail(error)

    def _fail(self, error: Exception) -> None:
        loop = self._loop

        if loop is not None:
            self._failure = error
            self._loop = None
            loop.call_soon_threadsafe(
                self._launch_failure_stop, self._generation
            )

    def _launch_failure_stop(self, generation: object) -> None:
        self._failure_task = asyncio.create_task(self._stop_failed(generation))

    async def _stop_failed(self, generation: object) -> None:
        try:
            async with self._lifecycle:
                if self._generation is generation:
                    await self._close()
        except Exception:
            logger.exception("StoreHost failed to close after a stream error")
