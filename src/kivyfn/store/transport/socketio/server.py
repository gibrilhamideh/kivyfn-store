# src/kivyfn/store/transport/socketio/server.py
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlunsplit

import socketio
from aiohttp import web
from kivyfn.logging import get_logger

from kivyfn.store.codec.events import encode_event_message
from kivyfn.store.codec.requests import decode_request
from kivyfn.store.codec.responses import encode_response
from kivyfn.store.error import StoreConnectionError, StoreStateError
from kivyfn.store.protocol.message.event import EventMessage
from kivyfn.store.transport.host import HostTransport

if TYPE_CHECKING:
    from kivyfn.store.host.host import StoreHost


logger = get_logger(__name__)


@dataclass(slots=True)
class _Connection:
    requests: asyncio.Lock = field(default_factory=asyncio.Lock)
    writes: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed: bool = False


class SocketIOServerTransport(HostTransport):
    """Carry typed Store messages for the host injected into start()."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        socketio_path: str = "socket.io",
        cors_allowed_origins: str | list[str] | None = None,
        logger: bool = False,
        engineio_logger: bool = False,
    ) -> None:
        self.host = host
        self.port = port
        self.socketio_path = socketio_path.strip("/")
        self._bind_port = port
        self._cors_allowed_origins = cors_allowed_origins
        self._logger = logger
        self._engineio_logger = engineio_logger
        self._connections: dict[str, _Connection] = {}
        self._lifecycle = asyncio.Lock()
        self._runner: web.AppRunner | None = None
        self._running = False
        self._create_server()

    async def start(self, store_host: StoreHost) -> None:
        """Bind the StoreHost and start accepting clients."""
        async with self._lifecycle:
            if self.running:
                if self.store_host is not store_host:
                    raise StoreStateError("Transport has another running host")
                return

            self.store_host = store_host
            runner = web.AppRunner(self.application)
            try:
                await runner.setup()
                site = web.TCPSite(
                    runner, host=self.host, port=self._bind_port
                )
                await site.start()
            except BaseException:
                await self._cleanup(runner)
                raise

            self._runner = runner
            self.port = runner.addresses[0][1]
            self._running = True

    async def stop(self) -> None:
        async with self._lifecycle:
            runner = self._runner
            if runner is None:
                return

            self._running = False
            try:
                for site in runner.sites:
                    await site.stop()
            finally:
                await self._cleanup(runner)

    async def emit(self, client_id: str, message: EventMessage) -> None:
        connection = self._connections.get(client_id)
        if not self.running or connection is None:
            raise StoreConnectionError("Store client is disconnected")

        async with connection.writes:
            if connection.closed:
                raise StoreConnectionError("Store client is disconnected")
            await self.sio.emit(
                "message", encode_event_message(message), to=client_id
            )

    async def disconnect(self, client_id: str) -> None:
        await self.sio.disconnect(client_id)

    async def on_connect(
        self, client_id: str, environ: dict[str, Any], auth: object = None
    ) -> bool:
        if not self.running:
            return False

        connection = _Connection()
        self._connections[client_id] = connection
        try:
            async with connection.requests:
                await self.store_host.connect(client_id)
        except Exception:
            logger.exception("Store client registration failed: %s", client_id)
            await self.on_disconnect(client_id)
            return False

        if not self.running or connection.closed:
            await self.on_disconnect(client_id)
            return False
        return True

    async def on_disconnect(
        self, client_id: str, reason: object = None
    ) -> None:
        connection = self._connections.pop(client_id, None)
        if connection is None:
            return

        connection.closed = True
        async with connection.requests:
            try:
                await self.store_host.disconnect(client_id)
            except Exception:
                logger.exception("Store client cleanup failed: %s", client_id)

    async def on_message(
        self, client_id: str, *values: object
    ) -> dict[str, Any] | None:
        connection = self._connections.get(client_id)
        if connection is None:
            return None

        try:
            async with connection.requests:
                if not self.running or connection.closed:
                    return None

                payload = values[0] if len(values) == 1 else values
                request = decode_request(payload)
                response = await self.store_host.request(client_id, request)
                return encode_response(response)
        except Exception:
            logger.exception("Store message handling failed: %s", client_id)

        # Host cleanup waits for the request lock, so release it first.
        await self.disconnect(client_id)
        return None

    @property
    def running(self) -> bool:
        return self._running

    @property
    def endpoint(self) -> dict[str, Any]:
        host = self.host
        if host == "0.0.0.0":
            host = "127.0.0.1"
        elif host == "::":
            host = "::1"
        if ":" in host:
            host = f"[{host}]"
        endpoint = urlunsplit(
            ("http", f"{host}:{self.port}", f"/{self.socketio_path}", "", "")
        )
        return {
            "transport": "socketio",
            "host": self.host,
            "port": self.port,
            "socketio_path": self.socketio_path,
            "endpoint": endpoint,
        }

    def _create_server(self) -> None:
        self.sio = socketio.AsyncServer(
            async_mode="aiohttp",
            async_handlers=False,
            cors_allowed_origins=self._cors_allowed_origins,
            logger=self._logger,
            engineio_logger=self._engineio_logger,
        )
        self.application = web.Application()
        self.sio.attach(self.application, socketio_path=self.socketio_path)
        self.sio.on("connect", self.on_connect)
        self.sio.on("disconnect", self.on_disconnect)
        self.sio.on("message", self.on_message)

    async def _cleanup(self, runner: web.AppRunner) -> None:
        try:
            await self.sio.shutdown()
            connections = tuple(self.sio.eio.sockets.values())
            # A polling peer may never return to drain its outgoing queue.
            await asyncio.gather(
                *(
                    connection.close(
                        wait=False,
                        reason=self.sio.eio.reason.SERVER_DISCONNECT,
                    )
                    for connection in connections
                )
            )
            # Engine.IO's sentinel also stops each WebSocket writer.
            for connection in connections:
                connection.queue.put_nowait(None)
            self.sio.eio.sockets.clear()
        finally:
            try:
                await runner.cleanup()
            finally:
                self._running = False
                self._runner = None
                self._connections.clear()
                self._create_server()
