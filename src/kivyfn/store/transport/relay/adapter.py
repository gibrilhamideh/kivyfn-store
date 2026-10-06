from __future__ import annotations

import asyncio
import inspect
import random
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from kivyfn.store.builder.relay import RelayConfig
from kivyfn.store.error import (
    StoreConnectionError,
    StoreStateError,
    StoreTimeoutError,
)
from kivyfn.store.transport.relay.session import RelaySession

if TYPE_CHECKING:
    from kivyfn.store.host.host import StoreHost


class SocketIORelay:
    """Host-owned outward connection; local service never waits for WAN.

    Each attempt uses a fresh connection generation and fresh credentials.
    Remote subscriptions live in StoreHost, just like local subscriptions.
    This adapter never caches state or retries Store requests.
    """

    def __init__(self, config: RelayConfig) -> None:
        self.config = config
        endpoint = urlsplit(config.endpoint)
        self.url = endpoint._replace(path="").geturl()
        self.socketio_path = endpoint.path.strip("/") or "socket.io"
        self._host: StoreHost | None = None
        self._session: RelaySession | None = None
        self._task: asyncio.Task[None] | None = None
        self._closing: asyncio.Task[None] | None = None
        self._changed = asyncio.Event()
        self._running = False
        self._last_error: Exception | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def running(self) -> bool:
        return self._running

    @property
    def online(self) -> bool:
        session = self._session
        return bool(
            self.running
            and session is not None
            and session.ready.is_set()
            and not session.lost.is_set()
        )

    @property
    def last_error(self) -> Exception | None:
        """A sanitized connection error; credentials are never included."""
        return self._last_error

    async def start(self, host: StoreHost) -> None:
        self._require_loop()
        if self._closing is not None:
            await asyncio.shield(self._closing)
        if self.running:
            if self._host is not host:
                raise StoreStateError("Relay already belongs to another host")
            return
        if not host.running:
            raise StoreStateError("Relay requires a running StoreHost")
        self._loop = asyncio.get_running_loop()
        self._host = host
        self._running = True
        self._last_error = None
        self._closing = None
        self._task = asyncio.create_task(self._run(), name="store-relay")
        self._task.add_done_callback(self._observe_run)

    def _observe_run(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            self._last_error = StoreConnectionError("Relay cleanup failed")

    async def stop(self) -> None:
        self._require_loop()
        if self._closing is None:
            self._running = False
            self._notify()
            task = self._task
            if task is not None:
                task.cancel()
            self._closing = asyncio.create_task(self._finish_stop(task))
        await asyncio.shield(self._closing)

    async def _finish_stop(self, task: asyncio.Task[None] | None) -> None:
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        self._task = None
        self._host = None

    async def wait_online(self, timeout: float | None = None) -> None:
        """Wait for registration; cancelling a waiter does not stop retries."""
        self._require_loop()
        try:
            async with asyncio.timeout(timeout):
                while not self.online:
                    if not self.running:
                        raise StoreStateError("Relay is stopped")
                    changed = self._changed
                    await changed.wait()
        except TimeoutError as error:
            raise StoreTimeoutError("Relay did not become online") from error

    def _notify(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()

    def _require_loop(self) -> None:
        if self._loop is not None and self._loop is not (
            asyncio.get_running_loop()
        ):
            raise StoreStateError("Relay belongs to another event loop")

    async def _credentials(self) -> dict[str, Any]:
        auth = self.config.auth
        value = auth() if callable(auth) else auth
        if inspect.isawaitable(value):
            value = await value
        if value is None and auth is None:
            return {}
        if not isinstance(value, Mapping):
            raise TypeError("Relay auth provider must return a mapping")
        return dict(value)

    async def _run(self) -> None:
        assert self._host is not None
        try:
            while self.running:
                session = RelaySession(self, self._host)
                self._session = session
                phase = "authentication"
                try:
                    async with asyncio.timeout(self.config.connect_timeout):
                        credentials = await self._credentials()
                        phase = "connection or registration"
                        await session.connect(credentials)
                    self._last_error = None
                    session.ready.set()
                    self._notify()
                    await session.lost.wait()
                    self._last_error = session.error
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Providers and servers can put secrets in exception text.
                    self._last_error = StoreConnectionError(
                        f"Relay {phase} failed"
                    )
                finally:
                    self._session = None
                    self._notify()
                    await session.close()
                if self.running:
                    delay = self.config.reconnect_interval
                    await asyncio.sleep(delay * random.uniform(0.8, 1.2))
        finally:
            self._running = False
            self._notify()
