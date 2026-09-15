# src/kivyfn/store/transport/socketio/client.py
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import socketio
from kivyfn.logging import get_logger
from socketio.exceptions import BadNamespaceError
from socketio.exceptions import ConnectionError as SocketIOConnectionError
from socketio.exceptions import TimeoutError as SocketIOTimeoutError

from kivyfn.store.codec.events import decode_event_message
from kivyfn.store.codec.requests import encode_request
from kivyfn.store.codec.responses import decode_response
from kivyfn.store.error import (
    StoreConnectionError,
    StoreProtocolError,
    StoreTimeoutError,
)
from kivyfn.store.protocol.message.requests import RequestBatch
from kivyfn.store.protocol.message.responses import ResponseBatch
from kivyfn.store.transport.client import ClientTransport

if TYPE_CHECKING:
    from kivyfn.store.client.client import StoreClient


logger = get_logger(__name__)


class SocketIOClientTransport(ClientTransport):
    """Carry Store messages between Socket.IO and one StoreClient."""

    def __init__(
        self,
        url: str,
        client: StoreClient,
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
        self._client = client

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
        self._connecting: asyncio.Task[None] | None = None
        self._pending: asyncio.Task[object] | None = None

        self._lifecycle = asyncio.Lock()
        self._requests = asyncio.Lock()
        self._messages = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        async with self._lifecycle:
            if self.connected:
                return

            if self.sio.eio.state != "disconnected":
                await self._disconnect_socket()

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
                self._drop_connection(notify=False)
                await self._disconnect_socket()

                raise StoreTimeoutError("Store connection timed out") from exc

            except SocketIOConnectionError as exc:
                self._drop_connection(notify=False)
                await self._disconnect_socket()

                raise StoreConnectionError("Store connection failed") from exc

            except BaseException:
                self._drop_connection(notify=False)
                await self._disconnect_socket()
                raise

            finally:
                self._connecting = None

    async def disconnect(self) -> None:
        self._drop_connection(notify=False)

        if self._connecting is not None:
            self._connecting.cancel()

        async with self._lifecycle:
            await self._disconnect_socket()

    async def request(self, request: RequestBatch) -> ResponseBatch:
        generation = self._generation
        pending: asyncio.Task[object] | None = None

        try:
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

            raise StoreProtocolError("Invalid Store response") from exc

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

        self._client.on_connect()

    def on_disconnect(self, reason: object = None) -> None:
        del reason

        self._drop_connection(notify=self._connecting is None)

    async def on_message(self, *values: object) -> None:
        generation = self._generation

        async with self._messages:
            if not self.connected or generation != self._generation:
                return

            try:
                try:
                    payload = values[0] if len(values) == 1 else values
                    message = decode_event_message(payload)
                except (TypeError, ValueError) as error:
                    raise StoreProtocolError("Invalid Store event") from error
                self._client.on_message(message)
            except Exception as error:
                logger.exception("Store message delivery failed")
                await self._disconnect_lost(error)

    async def _disconnect_lost(self, error: Exception | None = None) -> None:
        self._drop_connection(notify=False)
        self._client.on_disconnect(error)
        await self.disconnect()

    def _drop_connection(self, *, notify: bool) -> None:
        connected = self._connected

        self._connected = False

        if not connected:
            return

        self._generation += 1

        if self._pending is not None:
            self._pending.cancel()

        if notify:
            self._client.on_disconnect()

    async def _disconnect_socket(self) -> None:
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
