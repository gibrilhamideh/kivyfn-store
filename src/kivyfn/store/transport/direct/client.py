# src/kivyfn/store/transport/direct/client.py
from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, cast

from kivyfn.store.codec.events import (
    decode_event_message,
    encode_event_message,
)
from kivyfn.store.codec.requests import decode_request, encode_request
from kivyfn.store.codec.responses import decode_response, encode_response
from kivyfn.store.error import StoreProtocolError
from kivyfn.store.protocol.message.event import EventMessage
from kivyfn.store.protocol.message.requests import RequestBatch
from kivyfn.store.protocol.message.responses import ResponseBatch
from kivyfn.store.protocol.version import PROTOCOL_VERSION
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
        self._generation = 0
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

        if self._host is not None:
            await self.disconnect()
        host = cast(DirectHostTransport, resolve_host(self._name))
        self._generation += 1
        generation = self._generation

        async def message(message: EventMessage) -> None:
            await self._handle_message(message, generation)

        async def close() -> None:
            await self._handle_host_close(generation)

        client_id = await host.connect_client(
            message, close, protocol_version=PROTOCOL_VERSION
        )

        self._host = host
        self._client_id = client_id
        self._connected = True

        self._client.on_connect()

    async def disconnect(self) -> None:
        host = self._host
        client_id = self._client_id

        self._clear_connection()

        if host is not None and client_id is not None:
            await host.disconnect_client(client_id)

    async def request(self, request: RequestBatch) -> ResponseBatch:
        host, client_id = self._require_connection()
        try:
            decoded = decode_request(deepcopy(encode_request(request)))
        except (TypeError, ValueError) as error:
            host.store_host.rejected(client_id, error)
            raise StoreProtocolError("Invalid Store request") from error
        response = await host.request(client_id, decoded)
        try:
            return decode_response(deepcopy(encode_response(response)))
        except (TypeError, ValueError) as error:
            raise StoreProtocolError("Invalid Store response") from error

    async def _handle_message(
        self, message: EventMessage, generation: int
    ) -> None:
        if generation != self._generation or not self._connected:
            return
        try:
            try:
                decoded = decode_event_message(
                    deepcopy(encode_event_message(message))
                )
            except (TypeError, ValueError) as error:
                raise StoreProtocolError("Invalid Store event") from error
            self._client.on_message(decoded)
        except Exception as error:
            # Retain the registration until cleanup has removed it at the Host.
            self._connected = False
            self._client.on_disconnect(error)
            raise

    async def _handle_host_close(self, generation: int) -> None:
        if generation != self._generation:
            return
        connected = self._connected
        self._clear_connection()
        if connected:
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
        self._generation += 1
        self._connected = False
        self._host = None
        self._client_id = None
