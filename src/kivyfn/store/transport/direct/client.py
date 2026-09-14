# src/kivyfn/store/transport/direct/client.py
from __future__ import annotations

from typing import TYPE_CHECKING, cast

from kivyfn.store.protocol.message.event import EventMessage
from kivyfn.store.protocol.message.requests import RequestBatch
from kivyfn.store.protocol.message.responses import ResponseBatch
from kivyfn.store.transport.client import ClientTransport
from kivyfn.store.transport.direct.host import DirectHostTransport
from kivyfn.store.transport.direct.registry import resolve_host


if TYPE_CHECKING:
    from kivyfn.store.client.client import StoreClient


class DirectClientTransport(ClientTransport):
    """In-process Store client transport."""

    def __init__(self, name: str, client: StoreClient) -> None:
        if not name:
            raise ValueError(
                "Direct Store endpoint name must be a non-empty string"
            )

        self._name = name
        self._client = client
        self._connected = False
        self._host: DirectHostTransport | None = None
        self._client_id: str | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        if self._connected:
            return

        host = cast(DirectHostTransport, resolve_host(self._name))

        client_id = await host.connect_client(
            self._handle_message, self._handle_host_close
        )

        self._host = host
        self._client_id = client_id
        self._connected = True

        self._client.on_connect()

    async def disconnect(self) -> None:
        if not self._connected:
            return

        host = self._host
        client_id = self._client_id

        self._clear_connection()

        if host is not None and client_id is not None:
            await host.disconnect_client(client_id)

    async def request(self, request: RequestBatch) -> ResponseBatch:
        host, client_id = self._require_connection()
        return await host.request(client_id, request)

    async def _handle_message(self, message: EventMessage) -> None:
        self._client.on_message(message)

    async def _handle_host_close(self) -> None:
        if not self._connected:
            return

        self._clear_connection()
        self._client.on_disconnect()

    def _require_connection(self) -> tuple[DirectHostTransport, str]:
        if (
            not self._connected
            or self._host is None
            or self._client_id is None
        ):
            raise RuntimeError(
                f"Direct Store client is not connected: {self._name!r}"
            )

        return self._host, self._client_id

    def _clear_connection(self) -> None:
        self._connected = False
        self._host = None
        self._client_id = None
