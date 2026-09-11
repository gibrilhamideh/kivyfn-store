# src/kivyfn/store/transport/socketio/client.py
from __future__ import annotations

import asyncio

import socketio
from socketio.exceptions import (
    BadNamespaceError,
    ConnectionError as SocketIOConnectionError,
    TimeoutError as SocketIOTimeoutError,
)

from kivyfn.logging import get_logger
from kivyfn.store.codec.events import decode_event_message
from kivyfn.store.codec.requests import encode_request
from kivyfn.store.codec.responses import decode_response
from kivyfn.store.error import (
    StoreConnectionError,
    StoreProtocolError,
    StoreTimeoutError,
)
from kivyfn.store.protocol.message.requests import Request
from kivyfn.store.protocol.message.responses import Response
from kivyfn.store.transport.client import (
    ClientDisconnectHandler,
    ClientEventHandler,
    ClientTransport,
)


logger = get_logger(__name__)


class SocketIOClientTransport(ClientTransport):
    """Carry typed Store messages; StoreClient owns reconnect and resume."""

    def __init__(
        self,
        url: str,
        *,
        socketio_path: str = "socket.io",
        connect_timeout: int = 10,
        request_timeout: int = 10,
        logger: bool = False,
        engineio_logger: bool = False,
    ) -> None:
        self.url = url
        self.socketio_path = socketio_path.strip("/")
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout
        self.sio = socketio.AsyncClient(
            reconnection=False,
            handle_sigint=False,
            logger=logger,
            engineio_logger=engineio_logger,
            request_timeout=connect_timeout,
        )
        self.sio.on("connect", self.on_connect)
        self.sio.on("disconnect", self.on_disconnect)
        self.sio.on("message", self.on_message)

        self._connected = False
        self._generation = 0
        self._event_handler: ClientEventHandler | None = None
        self._disconnect_handler: ClientDisconnectHandler | None = None
        self._connecting: asyncio.Task[None] | None = None
        self._pending: asyncio.Task[object] | None = None
        self._notification: asyncio.Task[None] | None = None
        self._lifecycle = asyncio.Lock()
        self._requests = asyncio.Lock()
        self._events = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(
        self,
        event_handler: ClientEventHandler,
        disconnect_handler: ClientDisconnectHandler,
    ) -> None:
        async with self._lifecycle:
            if self.connected:
                return

            if self.sio.eio.state != "disconnected":
                await self._disconnect_socket()

            self._event_handler = event_handler
            self._disconnect_handler = disconnect_handler
            self._connecting = asyncio.create_task(
                self.sio.connect(
                    self.url,
                    socketio_path=self.socketio_path,
                    wait_timeout=self.connect_timeout,
                )
            )
            try:
                async with asyncio.timeout(self.connect_timeout):
                    await self._connecting
                if not self.connected:
                    raise StoreConnectionError("Store connection closed")
            except TimeoutError as exc:
                await self._disconnect_socket()
                raise StoreTimeoutError("Store connection timed out") from exc
            except SocketIOConnectionError as exc:
                await self._disconnect_socket()
                raise StoreConnectionError("Store connection failed") from exc
            except BaseException:
                await self._disconnect_socket()
                raise
            finally:
                self._connecting = None

    async def disconnect(self) -> None:
        self.on_disconnect()
        if self._connecting is not None:
            self._connecting.cancel()

        async with self._lifecycle:
            await self._disconnect_socket()

        notification = self._notification
        if (
            notification is not None
            and notification is not asyncio.current_task()
        ):
            await asyncio.shield(notification)

    async def request(self, request: Request) -> Response:
        """Encode a Request and decode its acknowledgement into a Response."""
        generation = self._generation
        pending: asyncio.Task[object] | None = None
        try:
            # Socket.IO calls must be serialized, including multipart messages.
            async with self._requests:
                if not self.connected or generation != self._generation:
                    raise StoreConnectionError("Store client is disconnected")

                pending = asyncio.create_task(
                    self.sio.call(
                        "message",
                        encode_request(request),
                        timeout=self.request_timeout,
                    )
                )
                self._pending = pending
                try:
                    payload = await pending
                finally:
                    self._pending = None

            return decode_response(payload)
        except SocketIOTimeoutError as exc:
            await self.disconnect()
            raise StoreTimeoutError("Store request timed out") from exc
        except (SocketIOConnectionError, BadNamespaceError) as exc:
            await self.disconnect()
            raise StoreConnectionError("Store connection was lost") from exc
        except (TypeError, ValueError) as exc:
            await self.disconnect()
            raise StoreProtocolError("Invalid Store message") from exc
        except asyncio.CancelledError:
            caller = asyncio.current_task()
            if caller is not None and caller.cancelling():
                if pending is not None:
                    await self.disconnect()
                raise
            raise StoreConnectionError("Store connection was lost") from None

    def on_connect(self) -> None:
        self._generation += 1
        self._connected = True

    def on_disconnect(self, reason: object = None) -> None:
        if not self._connected:
            return

        self._connected = False
        self._generation += 1
        if self._pending is not None:
            self._pending.cancel()

        handler = self._disconnect_handler
        self._event_handler = None
        self._disconnect_handler = None
        if handler is not None:
            self._notification = asyncio.create_task(self._notify(handler))

    async def on_message(self, *values: object) -> None:
        generation = self._generation
        async with self._events:
            handler = self._event_handler
            if handler is None or generation != self._generation:
                return

            try:
                payload = values[0] if len(values) == 1 else values
                await handler("message", decode_event_message(payload))
            except Exception:
                logger.exception("Store event delivery failed")
                await self.disconnect()

    async def _notify(self, handler: ClientDisconnectHandler) -> None:
        try:
            await handler()
        except Exception:
            logger.exception("Store disconnect handler failed")

    async def _disconnect_socket(self) -> None:
        # Flush the close packet, then stop the polling read without waiting
        # for another heartbeat. Engine.IO's normal disconnect waits for it.
        eio = self.sio.eio
        tasks = tuple(
            task
            for task in (eio.read_loop_task, eio.write_loop_task)
            if task is not None
        )
        disconnecting = asyncio.create_task(self.sio.disconnect())
        try:
            async with asyncio.timeout(self.connect_timeout):
                writer = eio.write_loop_task
                finished = writer if writer is not None else disconnecting
                await asyncio.shield(finished)
        except TimeoutError:
            pass
        finally:
            disconnecting.cancel()
            for task in tasks:
                task.cancel()
            await asyncio.gather(disconnecting, *tasks, return_exceptions=True)
            await eio.disconnect(abort=True)
            self.sio.callbacks.clear()
