# src/kivyfn/store/transport/direct/client.py
from __future__ import annotations

from typing import cast

from kivyfn.store.error import StoreConnectionError, StoreStateError
from kivyfn.store.protocol.message.requests import Request
from kivyfn.store.protocol.message.responses import Response
from kivyfn.store.transport.client import (
    ClientDisconnectHandler,
    ClientEventHandler,
    ClientTransport,
)
from kivyfn.store.transport.direct.host import DirectHostTransport
from kivyfn.store.transport.direct.registry import resolve_host


class DirectClientTransport(ClientTransport):
    """In-process Store client transport."""

    def __init__(self, name: str) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError(
                "Direct Store endpoint name must be a non-empty string"
            )

        self._name = name
        self._connected = False

        self._host: DirectHostTransport | None = None
        self._client_id: str | None = None

        self._event_handler: ClientEventHandler | None = None
        self._disconnect_handler: ClientDisconnectHandler | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(
        self,
        event_handler: ClientEventHandler,
        disconnect_handler: ClientDisconnectHandler,
    ) -> None:
        if self._connected:
            return

        try:
            host = cast(DirectHostTransport, resolve_host(self._name))
        except LookupError as error:
            raise StoreConnectionError(
                f"Direct Store endpoint is unavailable: {self._name!r}"
            ) from error

        try:
            client_id = await host.connect_client(
                self._handle_event, self._handle_host_close
            )
        except StoreStateError as error:
            raise StoreConnectionError(
                f"Direct Store endpoint is unavailable: {self._name!r}"
            ) from error

        self._host = host
        self._client_id = client_id
        self._event_handler = event_handler
        self._disconnect_handler = disconnect_handler
        self._connected = True

    async def disconnect(self) -> None:
        if not self._connected:
            return

        host = self._host
        client_id = self._client_id

        self._clear_connection()

        if host is not None and client_id is not None:
            await host.disconnect_client(client_id)

    async def request(self, request: Request) -> Response:
        host, client_id = self._require_connection()
        try:
            return await host.request(client_id, request)
        except StoreConnectionError:
            await self._handle_host_close()
            raise
        except StoreStateError as error:
            await self._handle_host_close()
            raise StoreConnectionError(
                f"Direct Store connection was lost: {self._name!r}"
            ) from error

    async def _handle_event(self, event: str, message: object | None) -> None:
        handler = self._event_handler

        if handler is None:
            return

        await handler(event, message)

    async def _handle_host_close(self) -> None:
        if not self._connected:
            return

        disconnect_handler = self._disconnect_handler

        self._clear_connection()

        if disconnect_handler is not None:
            await disconnect_handler()

    def _require_connection(self) -> tuple[DirectHostTransport, str]:
        if (
            not self._connected
            or self._host is None
            or self._client_id is None
        ):
            raise StoreConnectionError(
                f"Direct Store client is not connected: {self._name!r}"
            )

        return self._host, self._client_id

    def _clear_connection(self) -> None:
        self._connected = False
        self._host = None
        self._client_id = None
        self._event_handler = None
        self._disconnect_handler = None
