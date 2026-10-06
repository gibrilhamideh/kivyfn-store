from __future__ import annotations

import asyncio
from collections.abc import Callable
from importlib import import_module
from typing import TYPE_CHECKING, Any, Protocol, cast

from kivyfn.logging import get_logger

from kivyfn.store.error import (
    StoreInternalError,
    StoreProtocolError,
    StoreStateError,
)
from kivyfn.store.events import StoreCommit
from kivyfn.store.host.client import (
    HostClients,
    HostCloseHandler,
    HostEventSender,
)
from kivyfn.store.host.query import HostQueries
from kivyfn.store.host.request import HostRequests
from kivyfn.store.host.subscription import HostSubscriptions
from kivyfn.store.protocol.message.requests import RequestBatch
from kivyfn.store.protocol.message.responses import ResponseBatch
from kivyfn.store.protocol.version import PROTOCOL_VERSION
from kivyfn.store.store import Store
from kivyfn.store.transport.endpoint import create_host_transport
from kivyfn.store.transport.relay import SocketIORelay

if TYPE_CHECKING:
    from kivyfn.store.builder.config import DevToolsConfig, HostConfig

logger = get_logger(__name__)


class _DevTools(Protocol):
    @property
    def url(self) -> str: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class StoreHost:
    """Expose one Store through a transport."""

    def __init__(self, store: Store, config: HostConfig | None = None) -> None:
        config = store.config.host if config is None else config
        if config is None:
            raise ValueError("StoreHost requires a HostConfig")
        self.store = store
        self.config = config
        self.endpoint = config.endpoint
        self.devtools: _DevTools | None = None

        self.transport = create_host_transport(config.endpoint)
        self.relay = (
            None if config.relay is None else SocketIORelay(config.relay)
        )

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

    @property
    def devtools_url(self) -> str | None:
        return None if self.devtools is None else self.devtools.url

    async def start(self) -> None:
        self._require_loop()
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
                self.store.claim_host(self, asyncio.get_running_loop())
                self._unsubscribe = self.store.subscribe_commits(
                    self._on_commit
                )
                await self.transport.start(self)
                self._running = True
                if self.config.devtools is not False:
                    from kivyfn.store.builder.config import DevToolsConfig

                    config = (
                        DevToolsConfig()
                        if self.config.devtools is True
                        else self.config.devtools
                    )
                    devtools = self._create_devtools(config)
                    self.devtools = devtools
                    await devtools.start()
                if self.relay is not None:
                    await self.relay.start(self)
            except BaseException as error:
                self._failure = error
                try:
                    await self._close()
                finally:
                    self._finish_close()
                raise

    async def stop(self) -> None:
        self._require_loop()
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

    async def connect(
        self,
        client_id: str,
        *,
        send: HostEventSender | None = None,
        close: HostCloseHandler | None = None,
        protocol_version: int | None = None,
        max_pending: int | None = None,
    ) -> None:
        self._require_running()
        if (
            type(protocol_version) is not int
            or protocol_version != PROTOCOL_VERSION
        ):
            raise StoreProtocolError(
                f"Incompatible Store protocol: expected {PROTOCOL_VERSION}, "
                f"received {protocol_version!r}; use matching releases"
            )
        if self.clients.get(client_id) is not None:
            raise StoreStateError("Store client ID is already registered")
        self.clients.add(
            client_id,
            send=self.transport.emit if send is None else send,
            close=self.transport.disconnect if close is None else close,
            max_pending=max_pending,
        )
        self._capture("lifecycle", client_id, "connected")

    async def request(
        self, client_id: str, request: RequestBatch
    ) -> ResponseBatch:
        self._require_running()
        if self.clients.get(client_id) is None:
            raise StoreStateError("Store client is not registered")
        capture = self._capture("request", client_id, request)
        try:
            response = self.requests.handle(client_id, request)
            self._capture("response", capture, response)
            await self.clients.flush(client_id)
            return response
        except BaseException as error:
            self._capture(
                "failure",
                capture,
                error,
                cancelled=isinstance(error, asyncio.CancelledError),
            )
            raise

    async def disconnect(self, client_id: str) -> None:
        self._require_loop()
        self.subscriptions.remove_client(client_id)
        removed = await self.clients.remove(client_id)
        if removed is not None:
            self.store.close_history_group(client_id)
            self._capture("lifecycle", client_id, "disconnected")

    def rejected(self, client_id: str, error: Exception) -> None:
        self._capture("rejected", client_id, error)

    def _capture(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if self.devtools is None:
            return None
        try:
            return getattr(self.devtools, method)(*args, **kwargs)
        except Exception:
            logger.exception("Store diagnostic capture failed")
            return None

    def _create_devtools(self, config: DevToolsConfig) -> _DevTools:
        try:
            module = import_module("kivyfn.store.devtools")
        except ModuleNotFoundError as error:
            if error.name != "kivyfn.store.devtools":
                raise
            raise RuntimeError(
                "Store devtools are enabled but not installed. "
                "Install kivyfn-store[devtools]."
            ) from error

        devtools_type = getattr(module, "DevTools", None)
        if devtools_type is None:
            raise RuntimeError(
                "Installed store devtools package does not export DevTools"
            )

        return cast(_DevTools, devtools_type(self, config))

    def _require_running(self) -> None:
        if not self._running:
            raise StoreStateError("StoreHost is stopped")
        self._require_loop()

    def _require_loop(self) -> None:
        closed = self._closed
        if closed is not None and closed.get_loop() is not (
            asyncio.get_running_loop()
        ):
            raise StoreStateError("StoreHost belongs to another event loop")

    async def _close(self) -> None:
        self._running = False

        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

        self.subscriptions.clear()

        try:
            if self.devtools is not None:
                try:
                    await self.devtools.stop()
                finally:
                    self.devtools = None
        finally:
            try:
                if self.relay is not None:
                    await self.relay.stop()
            finally:
                try:
                    await self.clients.clear()
                finally:
                    try:
                        await self.transport.stop()
                    finally:
                        pending = tuple(self._client_closures)
                        if pending:
                            await asyncio.gather(
                                *pending, return_exceptions=True
                            )
                        self.store.release_host(self)

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
        client = self.clients.get(client_id)
        try:
            if client is not None and client.close_connection is not None:
                await client.close_connection(client_id)
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
